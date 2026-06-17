"""
Daily post-close signal review (runs at 15:00 CST, right after market close).

For every signal that fired today, reconstruct the traded contract's intraday 1-min
price path and store, in the signal_analysis table:
  - realized excursions: MFE (peak gain %), MAE (worst drawdown %), peak price/time
  - rule_pnl_pct: what the CURRENT live exit rule would have produced
  - a SUGGESTED management action (e.g. "take 30% + move stop to breakeven + trail")
    and suggested_pnl_pct: the % that suggestion would have captured on the real path

Price path source: option_level_bars (DB) first; if absent and a data_src is given
(AlpacaDataClient), fetch the traded contract's bars live. Underlying path + S/R
levels come from price_bars / oi_levels; expiry from option_chain_snapshots.
"""
import logging
from collections import defaultdict
from datetime import date as _date, timedelta

import psycopg2
from psycopg2.extras import execute_values
import config
from analysis.signal_detector import compute_exit_targets
from analysis.volume_analytics import (
    multitf_volumes, volume_shape_features, normalized_entropy as norm_entropy,
    chain_relative_volume, volume_migration,
)
from analysis.intent_inference import classify_intent, compute_lifecycle_pairs, _tod_frac
from data.alpaca_client import occ_symbol
from data.market_utils import CST
from output.discord_notifier import send_daily_review

logger = logging.getLogger(__name__)


def _conn():
    return psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD)


# ── exit / suggestion simulators on the option price path ──────────────────────

def _current_rule(styp, entry, opath, ubymin, e1, e2):
    """Current live exit: half at e1 (underlying), rest at e2, breakeven stop armed
    only AFTER e1, ride the remainder to EOD. No initial premium stop — the old
    -50% stop was removed (main.py) because 0DTE premium noise whipsawed it out of
    winners. (Single-day path: the next-day-expiry overnight hold for strong losers
    can't be reconstructed here, so a held remainder is valued at the day's EOD close.)"""
    stop, held, proc, e1done = None, 1.0, 0.0, False   # no stop active until e1 fills
    for (t, h, l, c) in opath[1:]:
        if held > 0 and stop is not None and l <= stop:
            proc += held * stop; held = 0.0; break
        u = ubymin.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            def hit(x): return x is not None and ((uh >= x) if styp == 'BULLISH' else (ul <= x))
            if not e1done and hit(e1):
                proc += 0.5 * c; held -= 0.5; e1done = True; stop = entry   # breakeven after e1
            if e1done and held > 0 and hit(e2):
                proc += held * c; held = 0.0; break
    if held > 0:
        proc += held * opath[-1][3]
    return (proc - entry) / entry * 100


def _ladder(entry, opath, legs, stop_pct=None, trail_arm=None, trail_pct=None):
    """Scale out at option-price take-profit legs [(gain_pct, qty_frac)]; remainder trails
    (if set) or rides to EOD. No initial premium stop by default (stop_pct=None) — aligned
    with the live rule, which dropped the -50% stop; an optional hard stop is honored if a
    caller passes stop_pct, and the trailing stop still arms after trail_arm is reached."""
    stop = entry * (1 - stop_pct) if stop_pct else None
    held, proc, peak = 1.0, 0.0, entry
    legs = sorted(legs); li = 0
    for (t, h, l, c) in opath[1:]:
        if held > 0 and stop is not None and l <= stop:
            proc += held * stop; held = 0.0; break
        while li < len(legs) and held > 0 and h >= entry * (1 + legs[li][0]):
            q = min(legs[li][1], held); proc += q * entry * (1 + legs[li][0]); held -= q; li += 1
        peak = max(peak, h)
        if trail_arm and trail_pct and held > 0 and peak >= entry * (1 + trail_arm):
            ts = peak * (1 - trail_pct)
            if l <= ts:
                proc += held * ts; held = 0.0; break
    if held > 0:
        proc += held * opath[-1][3]
    return (proc - entry) / entry * 100


def _suggest(entry, opath):
    """Return (action, suggested_pnl_pct, human_text) from the realized path, tiered by MFE."""
    mfe = (max(b[1] for b in opath[1:]) / entry - 1) if len(opath) > 1 else 0.0
    mfe_pct = mfe * 100
    if mfe < 0.10:
        action = 'NO_RUNNER'
        pnl = _ladder(entry, opath, [(0.15, 1.0)])      # scalp ~+15% if reached, else ride to EOD
        text = (f"Peak only +{mfe_pct:.0f}% - not enough thrust to manage; scalp small or "
                f"tighten entry. Consider exiting full near +15%.")
    elif mfe < 0.30:
        action = 'TAKE_FULL_~20'
        pnl = _ladder(entry, opath, [(0.20, 1.0)])
        text = f"Modest move (peak +{mfe_pct:.0f}%) - take full position near +20%; little runner."
    elif mfe < 0.80:
        action = 'TAKE_50@30_BE_TRAIL'
        pnl = _ladder(entry, opath, [(0.30, 0.5)], trail_arm=0.30, trail_pct=0.30)
        text = (f"Take 30-50% off at +30%, move stop to breakeven, trail the rest "
                f"(runner peaked +{mfe_pct:.0f}%).")
    else:
        action = 'SCALE_33@50_TRAIL'
        pnl = _ladder(entry, opath, [(0.50, 0.34)], trail_arm=0.50, trail_pct=0.35)
        text = (f"Big runner (peak +{mfe_pct:.0f}%): bank ~1/3 at +50%, move stop to "
                f"breakeven, trail the remainder ~35% to ride the move.")
    return action, pnl, text


def _outcome_labels(signal_time, opath: list) -> dict:
    """
    Objective outcome labels (spec §20-§24, §71) from the traded contract's path after entry.
    opath: [(bar_time, high, low, close), ...] starting at the entry bar.
    Additive: all original keys are preserved; new keys are append-only.
    """
    entry = float(opath[0][3])
    fwd = opath[1:]

    def ret_at(mins):
        cutoff = signal_time + timedelta(minutes=mins)
        after = [b for b in fwd if b[0] >= cutoff]
        c = float(after[0][3]) if after else float(opath[-1][3])
        return round((c / entry - 1) * 100, 2)

    def first_touch(up, dn, mins):
        """Which threshold is hit first within `mins`: 'up', 'down', or None (stop wins ties)."""
        cutoff = signal_time + timedelta(minutes=mins)
        for (t, h, l, c) in fwd:
            if t > cutoff:
                break
            if float(l) <= entry * (1 - dn):
                return 'down'
            if float(h) >= entry * (1 + up):
                return 'up'
        return None

    highs = [float(b[1]) for b in fwd]; lows = [float(b[2]) for b in fwd]
    mfe = (max(highs) / entry - 1) * 100 if highs else 0.0
    mae = (min(lows) / entry - 1) * 100 if lows else 0.0

    peak_val   = max(highs) if highs else entry
    trough_val = min(lows)  if lows  else entry
    peak_bar   = next((b for b in fwd if float(b[1]) >= peak_val),   None)
    trough_bar = next((b for b in fwd if float(b[2]) <= trough_val), None)
    t_mfe = round((peak_bar[0]   - signal_time).total_seconds() / 60) if peak_bar   else None
    t_mae = round((trough_bar[0] - signal_time).total_seconds() / 60) if trough_bar else None

    return dict(
        entry_price=round(entry, 4),
        return_1m=ret_at(1), return_3m=ret_at(3),
        return_5m=ret_at(5), return_15m=ret_at(15), return_30m=ret_at(30),
        return_60m=ret_at(60), return_eod=round((float(opath[-1][3]) / entry - 1) * 100, 2),
        mfe_pct=round(mfe, 2), mae_pct=round(mae, 2),
        time_to_mfe_min=t_mfe, time_to_mae_min=t_mae,
        reached_25pct=mfe >= 25, reached_50pct=mfe >= 50,
        reached_100pct=mfe >= 100, reached_200pct=mfe >= 200, reached_500pct=mfe >= 500,
        entry_success=(first_touch(0.50, 0.35, 30) == 'up'),
        strong_entry_success=(first_touch(1.00, 0.35, 60) == 'up'),
        false_positive=(first_touch(0.25, 0.35, 30) == 'down'),
    )


# ── Phase 1 helper functions (§57-§69, §72) ────────────────────────────────────

def _day_extremes(ob_full: list, ei: int) -> dict:
    """§57: Option contract's session-wide extremes and pre-alert low."""
    if not ob_full:
        return {'absolute_day_low': None, 'absolute_day_low_time': None,
                'absolute_day_high': None, 'absolute_day_high_time': None,
                'pre_alert_low': None}
    min_bar = min(ob_full, key=lambda b: float(b[2]))
    max_bar = max(ob_full, key=lambda b: float(b[1]))
    pre = ob_full[:ei + 1] if ei is not None and ei >= 0 else []
    pre_low = min((float(b[2]) for b in pre), default=None)
    return {
        'absolute_day_low':       round(float(min_bar[2]), 4),
        'absolute_day_low_time':  min_bar[0],
        'absolute_day_high':      round(float(max_bar[1]), 4),
        'absolute_day_high_time': max_bar[0],
        'pre_alert_low':          round(pre_low, 4) if pre_low is not None else None,
    }


def _excursion_timing(opath: list, signal_time) -> dict:
    """§58/59: Post-alert excursion timing and drawdown depth."""
    if not opath or len(opath) < 2:
        return {'post_alert_low': None, 'post_alert_low_time': None,
                'draw_down_magnitude_pct': 0.0, 'time_to_mae_min': None,
                'time_to_mfe_min': None, 'time_underwater_min': 0}
    entry = opath[0][3]
    fwd = opath[1:]
    min_bar = min(fwd, key=lambda b: b[2])
    max_bar = max(fwd, key=lambda b: b[1])
    mae_ratio = min_bar[2] / entry - 1
    dd = round(abs(mae_ratio) * 100, 2) if mae_ratio < 0 else 0.0
    t_mae = round((min_bar[0] - signal_time).total_seconds() / 60)
    t_mfe = round((max_bar[0] - signal_time).total_seconds() / 60)
    return {
        'post_alert_low':          round(min_bar[2], 4),
        'post_alert_low_time':     min_bar[0],
        'draw_down_magnitude_pct': dd,
        'time_to_mae_min':         t_mae,
        'time_to_mfe_min':         t_mfe,
        'time_underwater_min':     sum(1 for b in fwd if b[3] < entry),
    }


def _entry_timing_quality(entry: float, abs_day_low, draw_down_pct: float,
                           mfe_pct: float) -> dict:
    """§64: How close to the session low the entry was, plus early/bad entry flags."""
    if abs_day_low and abs_day_low > 0 and entry > 0:
        pct_above = round((entry - abs_day_low) / abs_day_low, 4)
        score = round(max(0.0, 1.0 - min(pct_above, 1.0)), 4)
        label = ('EXCELLENT' if pct_above <= 0.15 else
                 'GOOD'      if pct_above <= 0.30 else
                 'ACCEPTABLE' if pct_above <= 0.50 else 'CHASED')
    else:
        pct_above = score = label = None
    return {
        'entry_above_lod_pct':        pct_above,
        'entry_timing_score':          score,
        'entry_timing_label':          label,
        'possible_early_entry':        draw_down_pct >= 35 and mfe_pct >= 50,
        'strong_early_entry_warning':  draw_down_pct >= 50 and mfe_pct > 0,
        'possible_bad_entry':          draw_down_pct >= 35 and mfe_pct < 25,
    }


def _capture_metrics(rule_pnl: float, mfe: float) -> dict:
    """§62/63: Profit capture efficiency. blended_return_pct uses rule_pnl (simulation-based)."""
    blended = round(rule_pnl, 2)
    if mfe > 0.01:
        eff = round(min(max(rule_pnl / mfe, 0.0), 1.0), 4)
        left = round(mfe - rule_pnl, 2)
        cap_label = ('EXCELLENT' if eff >= 0.80 else 'GOOD' if eff >= 0.60 else
                     'PARTIAL'   if eff >= 0.40 else 'LOW')
    else:
        eff = left = cap_label = None
    return {'blended_return_pct': blended, 'profit_capture_efficiency': eff,
            'profit_left_on_table_pct': left, 'capture_label': cap_label}


def _rr_ratios(mfe: float, mae: float, blended: float) -> dict:
    """§65: Ex-post (MFE/|MAE|) and realized (blended/|MAE|) reward-to-risk."""
    abs_risk = abs(mae) if mae < 0 else 0.01
    return {
        'ex_post_rr_ratio':  round(mfe / max(abs_risk, 0.01), 2) if mfe > 0 else 0.0,
        'realized_rr_ratio': round(max(blended, 0.0) / max(abs_risk, 0.01), 2),
    }


def _target_hits(ubymin: dict, styp: str, signal_time, e1, e2) -> dict:
    """§66: Find the first bar after signal_time where each underlying target was crossed."""
    t1_time = t2_time = None
    for bt in sorted(ubymin):
        if bt < signal_time:
            continue
        uh, ul = ubymin[bt]
        if t1_time is None and e1 is not None:
            if (uh >= e1) if styp == 'BULLISH' else (ul <= e1):
                t1_time = bt
        if t2_time is None and e2 is not None:
            if (uh >= e2) if styp == 'BULLISH' else (ul <= e2):
                t2_time = bt
        if t1_time and t2_time:
            break
    return {
        'target1_reached':       t1_time is not None,
        'target1_reached_time':  t1_time,
        'target2_reached':       t2_time is not None,
        'target2_reached_time':  t2_time,
        'target1_capture_label': 'HIT' if t1_time else ('MISS' if e1 else 'NO_TARGET'),
        'target2_capture_label': 'HIT' if t2_time else ('MISS' if e2 else 'NO_TARGET'),
    }


# ── counterfactual sub-simulators ────────────────────────────────────────────

def _cf_sell_at_target(entry, opath, ubymin, styp, target):
    held, proc = 1.0, 0.0
    for (t, h, l, c) in opath[1:]:
        u = ubymin.get(t.replace(second=0, microsecond=0))
        if target is not None and u:
            uh, ul = u
            if (uh >= target) if styp == 'BULLISH' else (ul <= target):
                proc += held * c; held = 0.0; break
    proc += held * opath[-1][3]
    return (proc - entry) / entry * 100


def _cf_half_t1_half_t2(entry, opath, ubymin, styp, e1, e2):
    held, proc, e1done = 1.0, 0.0, False
    for (t, h, l, c) in opath[1:]:
        u = ubymin.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            if not e1done and e1 is not None:
                if (uh >= e1) if styp == 'BULLISH' else (ul <= e1):
                    proc += 0.5 * c; held -= 0.5; e1done = True
            if e1done and held > 0 and e2 is not None:
                if (uh >= e2) if styp == 'BULLISH' else (ul <= e2):
                    proc += held * c; held = 0.0; break
    proc += held * opath[-1][3]
    return (proc - entry) / entry * 100


def _cf_trailing_stop(entry, opath, trail_pct=0.20):
    held, proc, peak = 1.0, 0.0, entry
    stop = entry * (1 - trail_pct)
    for (t, h, l, c) in opath[1:]:
        peak = max(peak, h); stop = max(stop, peak * (1 - trail_pct))
        if l <= stop:
            proc += held * stop; held = 0.0; break
    proc += held * opath[-1][3]
    return (proc - entry) / entry * 100


def _cf_time_exit(entry, opath, signal_time, minutes):
    cutoff = signal_time + timedelta(minutes=minutes)
    for (t, h, l, c) in opath[1:]:
        if t >= cutoff:
            return (c / entry - 1) * 100
    return (opath[-1][3] / entry - 1) * 100


def _counterfactuals(entry, opath, ubymin, styp, e1, e2,
                     rule_pnl: float, mfe: float, signal_time) -> list:
    """§68: 8 alternative exit strategies on the realized path vs CURRENT_RULE baseline."""
    strats = [
        ('SELL_ALL_T1',    _cf_sell_at_target(entry, opath, ubymin, styp, e1)),
        ('HALF_T1_HALF_T2', _cf_half_t1_half_t2(entry, opath, ubymin, styp, e1, e2)),
        ('SELL_ALL_T2',    _cf_sell_at_target(entry, opath, ubymin, styp, e2)),
        ('HOLD_EOD',       (opath[-1][3] / entry - 1) * 100),
        ('CURRENT_RULE',   rule_pnl),
        ('TRAILING_20PCT', _cf_trailing_stop(entry, opath, trail_pct=0.20)),
        ('TIME_30MIN',     _cf_time_exit(entry, opath, signal_time, 30)),
        ('TIME_60MIN',     _cf_time_exit(entry, opath, signal_time, 60)),
    ]
    return [{'strategy': s, 'return_pct': round(r, 2),
             'capture_efficiency': round(r / mfe, 4) if mfe > 0.01 else None,
             'diff_from_actual': round(r - rule_pnl, 2)}
            for s, r in strats]


def _entry_delays(opath: list, signal_time, styp: str, ubymin: dict,
                  e1, e2) -> list:
    """§69: Simulate entering 1/2/3/5 min later and re-running current rule."""
    orig_entry = opath[0][3]
    results = []
    for delay in (1, 2, 3, 5):
        cutoff = signal_time + timedelta(minutes=delay)
        delayed_bar = next((b for b in opath if b[0] >= cutoff), None)
        if not delayed_bar or delayed_bar[3] <= 0:
            results.append({'delay_min': delay, 'delayed_entry_price': None,
                            'mfe_pct': None, 'mae_pct': None, 'rule_pnl_pct': None,
                            'capture_efficiency': None, 'move_missed_before_entry_pct': None})
            continue
        di = opath.index(delayed_bar)
        sub = opath[di:]
        de = delayed_bar[3]
        sub_highs = [b[1] for b in sub[1:]]
        sub_lows  = [b[2] for b in sub[1:]]
        mfe = (max(sub_highs) / de - 1) * 100 if sub_highs else 0.0
        mae = (min(sub_lows)  / de - 1) * 100 if sub_lows  else 0.0
        rule = _current_rule(styp, de, sub, ubymin, e1, e2)
        results.append({
            'delay_min': delay, 'delayed_entry_price': round(de, 4),
            'mfe_pct': round(mfe, 2), 'mae_pct': round(mae, 2),
            'rule_pnl_pct': round(rule, 2),
            'capture_efficiency': round(rule / mfe, 4) if mfe > 0.01 else None,
            'move_missed_before_entry_pct': round((de / orig_entry - 1) * 100, 2),
        })
    return results


def _scan_missed_opps(analysis_date, cur) -> list:
    """§72: Option contracts in option_level_bars that moved >=100% within 30 min
    from any bar low during the session, without a matching fired signal."""
    from collections import defaultdict

    cur.execute("""
        SELECT occ_symbol, symbol, strike, option_type, level_type, rank,
               bar_time, low, close
        FROM option_level_bars WHERE level_date = %s
        ORDER BY occ_symbol, bar_time
    """, (analysis_date,))
    bars_by_occ: dict = defaultdict(list)
    meta_by_occ: dict = {}
    for occ, sym, strike, otype, ltype, rank, bt, lo, cl in cur.fetchall():
        bars_by_occ[occ].append((bt, float(lo), float(cl)))
        meta_by_occ[occ] = (sym, float(strike), otype, ltype or '', rank or 0)

    cur.execute("""
        SELECT DISTINCT symbol, option_type, traded_strike::numeric
        FROM signals WHERE signal_time::date = %s AND traded_strike IS NOT NULL
    """, (analysis_date,))
    already_signaled = {(s, o, float(k)) for s, o, k in cur.fetchall()}

    try:
        cur.execute("""
            SELECT symbol, candidate_side, strike::numeric, blocked_reason
            FROM signal_candidates WHERE session_date = %s AND strike IS NOT NULL
        """, (analysis_date,))
        block_map = {(s, sd, float(k)): r for s, sd, k, r in cur.fetchall()}
    except Exception:
        block_map = {}

    missed = []
    seen: set = set()
    for occ, bars in bars_by_occ.items():
        sym, strike, otype, ltype, rank = meta_by_occ[occ]
        if (sym, otype, strike) in already_signaled or occ in seen:
            continue
        for i, (bt, lo, _) in enumerate(bars):
            if lo <= 0:
                continue
            cutoff = bt + timedelta(minutes=30)
            future = [b[2] for b in bars[i + 1:] if b[0] <= cutoff]
            if not future:
                continue
            max_p = max(future)
            move = (max_p / lo - 1) * 100
            if move < 100.0:
                continue
            max_bt = next((b[0] for b in bars[i + 1:] if b[0] <= cutoff and b[2] == max_p), None)
            t_max = round((max_bt - bt).total_seconds() / 60) if max_bt else None
            missed.append({
                'session_date': analysis_date, 'symbol': sym, 'occ_symbol': occ,
                'strike': strike, 'option_type': otype, 'level_type': ltype, 'level_rank': rank,
                'event_start_time': bt, 'local_low_price': round(lo, 4),
                'maximum_price': round(max_p, 4), 'maximum_return_pct': round(move, 1),
                'time_to_max_min': t_max,
                'blocking_reason': block_map.get((sym, otype, strike), 'NOT_EVALUATED'),
            })
            seen.add(occ)
            break
    return missed


# ── main entry point ───────────────────────────────────────────────────────────

def analyze_daily_signals(analysis_date: _date, data_src=None, sheets=None) -> int:
    """Analyze all of analysis_date's signals -> signal_analysis / signal_outcomes /
    counterfactual_exits / entry_delay_study / missed_opportunities. Returns rows written."""
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute("""SELECT id, symbol, signal_time, signal_type, traded_strike,
                              option_type, trigger_price, level_price
                       FROM signals WHERE signal_time::date = %s ORDER BY signal_time""",
                    (analysis_date,))
        sigs = cur.fetchall()
        if not sigs:
            logger.info("Daily review %s: no signals to analyze", analysis_date)
            return 0

        cur.execute("""SELECT symbol, MIN(expiry_date) FROM option_chain_snapshots
                       WHERE snap_date = %s GROUP BY symbol""", (analysis_date,))
        expiry_map = {s: e for s, e in cur.fetchall()}

        rows: list[dict]  = []
        outcome_rows: list = []
        cf_rows: list      = []
        delay_rows: list   = []

        for sid, sym, st, styp, tstrike, otype, espot, lprice in sigs:
            tstrike = float(tstrike) if tstrike is not None else None
            if tstrike is None or otype is None:
                continue

            # option price path: DB first, then live Alpaca fallback
            cur.execute("""SELECT bar_time, high, low, close FROM option_level_bars
                           WHERE symbol=%s AND level_date=%s AND strike=%s AND option_type=%s
                           ORDER BY bar_time""", (sym, analysis_date, tstrike, otype))
            ob = cur.fetchall()
            source = 'option_bars'
            if not ob and data_src is not None:
                expiry = expiry_map.get(sym) or data_src.get_nearest_expiry(sym)
                if expiry:
                    occ = occ_symbol(sym, expiry, tstrike, otype)
                    fetched = data_src.get_option_bars(occ, count=config.SESSION_BARS)
                    ob = [(b['bar_time'], b['high'], b['low'], b['close']) for b in fetched]
                    source = 'alpaca_live'

            ei = next((i for i, b in enumerate(ob) if b[0] >= st), None)
            if ei is None or ei + 1 >= len(ob):
                # Guard: never overwrite an existing good row with NO_DATA NULLs.
                # A re-run that can't reconstruct the traded contract's path (e.g.
                # an expired 0DTE contract Alpaca no longer serves) must not destroy
                # the metrics a prior run already computed.
                cur.execute("SELECT 1 FROM signal_analysis "
                            "WHERE signal_id=%s AND entry_price IS NOT NULL", (sid,))
                if cur.fetchone():
                    logger.info("Daily review %s: no path for signal %s this run — "
                                "keeping existing metrics (not overwriting with NO_DATA)",
                                analysis_date, sid)
                    continue
                rows.append(dict(signal_id=sid, analysis_date=analysis_date, symbol=sym,
                                 signal_time=st, signal_type=styp, traded_strike=tstrike,
                                 option_type=otype, entry_price=None, mfe_pct=None, mae_pct=None,
                                 peak_price=None, peak_time=None, trough_price=None,
                                 rule_pnl_pct=None, suggested_action='NO_DATA',
                                 suggested_pnl_pct=None,
                                 suggestion='No intraday price path available for the traded contract.',
                                 data_source=source))
                continue

            opath = [(t, float(h), float(l), float(c)) for t, h, l, c in ob[ei:]]
            entry = opath[0][3]
            if entry <= 0:
                continue
            highs = [b[1] for b in opath[1:]]
            lows  = [b[2] for b in opath[1:]]
            peak   = max(highs) if highs else entry
            trough = min(lows)  if lows  else entry
            peak_i = ei + 1 + highs.index(peak) if highs else ei
            peak_time = ob[peak_i][0]
            mfe = (peak   / entry - 1) * 100
            mae = (trough / entry - 1) * 100

            # underlying path + targets
            cur.execute("""SELECT bar_time, high, low FROM price_bars
                           WHERE symbol=%s AND bar_time::date=%s AND spot_price IS NOT NULL
                           AND bar_time > %s ORDER BY bar_time""", (sym, analysis_date, st))
            ubymin = {bt.replace(second=0, microsecond=0): (float(h), float(l))
                      for bt, h, l in cur.fetchall()}
            cur.execute("""SELECT level_type, strike FROM oi_levels
                           WHERE symbol=%s AND level_date=%s""", (sym, analysis_date))
            levels = [{'level_type': lt, 'strike': float(s)} for lt, s in cur.fetchall()]
            e1, e2 = compute_exit_targets(styp, float(espot), levels) if levels else (None, None)
            rule_pnl = _current_rule(styp, entry, opath, ubymin, e1, e2)

            # ── Phase 1 metrics ────────────────────────────────────────────────
            ext  = _day_extremes(ob, ei)
            exc  = _excursion_timing(opath, st)
            qual = _entry_timing_quality(entry, ext['absolute_day_low'],
                                         exc['draw_down_magnitude_pct'], mfe)
            cap  = _capture_metrics(rule_pnl, mfe)
            rr   = _rr_ratios(mfe, mae, cap['blended_return_pct'])
            thit = _target_hits(ubymin, styp, st, e1, e2)

            for cf in _counterfactuals(entry, opath, ubymin, styp, e1, e2,
                                        rule_pnl, mfe, st):
                cf_rows.append((sid, analysis_date, cf['strategy'],
                                cf['return_pct'], cf['capture_efficiency'],
                                cf['diff_from_actual']))

            for dl in _entry_delays(opath, st, styp, ubymin, e1, e2):
                delay_rows.append((sid, analysis_date, dl['delay_min'],
                                   dl['delayed_entry_price'], dl['mfe_pct'], dl['mae_pct'],
                                   dl['rule_pnl_pct'], dl['capture_efficiency'],
                                   dl['move_missed_before_entry_pct']))

            # ── outcome labels + trade-quality metrics ─────────────────────────
            lab = _outcome_labels(st, opath)
            contract_lod = min((float(b[2]) for b in ob[:ei + 1]), default=entry)
            entry_vs_lod = round(entry / max(contract_lod, 0.01), 3)
            pct_peak = round(rule_pnl / mfe * 100, 1) if mfe >= 25 else None

            outcome_rows.append((
                sid, analysis_date, sym, lab['entry_price'],
                lab['return_5m'], lab['return_15m'], lab['return_30m'],
                lab['return_60m'], lab['return_eod'], lab['mfe_pct'], lab['mae_pct'],
                lab['reached_50pct'], lab['reached_100pct'], lab['reached_200pct'],
                lab['entry_success'], lab['strong_entry_success'], lab['false_positive'],
                round(contract_lod, 4), entry_vs_lod, pct_peak,
                lab['return_1m'], lab['return_3m'],
                lab['reached_25pct'], lab['reached_500pct'],
                lab['time_to_mfe_min'], lab['time_to_mae_min'],
            ))

            action, sug_pnl, text = _suggest(entry, opath)
            rows.append(dict(
                signal_id=sid, analysis_date=analysis_date, symbol=sym,
                signal_time=st, signal_type=styp, traded_strike=tstrike,
                option_type=otype, entry_price=round(entry, 4),
                mfe_pct=round(mfe, 2), mae_pct=round(mae, 2),
                peak_price=round(peak, 4), peak_time=peak_time,
                trough_price=round(trough, 4), rule_pnl_pct=round(rule_pnl, 2),
                suggested_action=action, suggested_pnl_pct=round(sug_pnl, 2),
                suggestion=text, data_source=source,
                **ext, **exc, **qual, **cap, **rr, **thit,
            ))

        # ── write signal_analysis (Phase 0 baseline + Phase 1 columns) ─────────
        _COLS = (
            'signal_id', 'analysis_date', 'symbol', 'signal_time', 'signal_type',
            'traded_strike', 'option_type', 'entry_price', 'mfe_pct', 'mae_pct',
            'peak_price', 'peak_time', 'trough_price', 'rule_pnl_pct', 'suggested_action',
            'suggested_pnl_pct', 'suggestion', 'data_source',
            'absolute_day_low', 'absolute_day_low_time',
            'absolute_day_high', 'absolute_day_high_time', 'pre_alert_low',
            'post_alert_low', 'post_alert_low_time', 'draw_down_magnitude_pct',
            'time_to_mfe_min', 'time_to_mae_min', 'time_underwater_min',
            'blended_return_pct', 'profit_capture_efficiency',
            'profit_left_on_table_pct', 'capture_label',
            'entry_above_lod_pct', 'entry_timing_score', 'entry_timing_label',
            'possible_early_entry', 'strong_early_entry_warning', 'possible_bad_entry',
            'ex_post_rr_ratio', 'realized_rr_ratio',
            'target1_reached', 'target1_reached_time',
            'target2_reached', 'target2_reached_time',
            'target1_capture_label', 'target2_capture_label',
        )
        ph = ','.join(['%s'] * len(_COLS))
        cur.executemany(f"""
            INSERT INTO signal_analysis
                ({','.join(_COLS)})
            VALUES ({ph})
            ON CONFLICT (signal_id) DO UPDATE SET
                analysis_date=EXCLUDED.analysis_date, entry_price=EXCLUDED.entry_price,
                mfe_pct=EXCLUDED.mfe_pct, mae_pct=EXCLUDED.mae_pct,
                peak_price=EXCLUDED.peak_price, peak_time=EXCLUDED.peak_time,
                trough_price=EXCLUDED.trough_price, rule_pnl_pct=EXCLUDED.rule_pnl_pct,
                suggested_action=EXCLUDED.suggested_action,
                suggested_pnl_pct=EXCLUDED.suggested_pnl_pct,
                suggestion=EXCLUDED.suggestion, data_source=EXCLUDED.data_source,
                absolute_day_low=EXCLUDED.absolute_day_low,
                absolute_day_low_time=EXCLUDED.absolute_day_low_time,
                absolute_day_high=EXCLUDED.absolute_day_high,
                absolute_day_high_time=EXCLUDED.absolute_day_high_time,
                pre_alert_low=EXCLUDED.pre_alert_low,
                post_alert_low=EXCLUDED.post_alert_low,
                post_alert_low_time=EXCLUDED.post_alert_low_time,
                draw_down_magnitude_pct=EXCLUDED.draw_down_magnitude_pct,
                time_to_mfe_min=EXCLUDED.time_to_mfe_min,
                time_to_mae_min=EXCLUDED.time_to_mae_min,
                time_underwater_min=EXCLUDED.time_underwater_min,
                blended_return_pct=EXCLUDED.blended_return_pct,
                profit_capture_efficiency=EXCLUDED.profit_capture_efficiency,
                profit_left_on_table_pct=EXCLUDED.profit_left_on_table_pct,
                capture_label=EXCLUDED.capture_label,
                entry_above_lod_pct=EXCLUDED.entry_above_lod_pct,
                entry_timing_score=EXCLUDED.entry_timing_score,
                entry_timing_label=EXCLUDED.entry_timing_label,
                possible_early_entry=EXCLUDED.possible_early_entry,
                strong_early_entry_warning=EXCLUDED.strong_early_entry_warning,
                possible_bad_entry=EXCLUDED.possible_bad_entry,
                ex_post_rr_ratio=EXCLUDED.ex_post_rr_ratio,
                realized_rr_ratio=EXCLUDED.realized_rr_ratio,
                target1_reached=EXCLUDED.target1_reached,
                target1_reached_time=EXCLUDED.target1_reached_time,
                target2_reached=EXCLUDED.target2_reached,
                target2_reached_time=EXCLUDED.target2_reached_time,
                target1_capture_label=EXCLUDED.target1_capture_label,
                target2_capture_label=EXCLUDED.target2_capture_label,
                created_at=NOW()
        """, [tuple(r.get(c) for c in _COLS) for r in rows])

        # ── signal_outcomes (extended with Phase 1 columns) ─────────────────────
        if outcome_rows:
            cur.executemany("""
                INSERT INTO signal_outcomes
                    (signal_id, session_date, symbol, entry_price,
                     return_5m, return_15m, return_30m, return_60m, return_eod,
                     mfe_pct, mae_pct, reached_50pct, reached_100pct, reached_200pct,
                     entry_success, strong_entry_success, false_positive,
                     contract_lod, entry_vs_lod, pct_peak_captured,
                     return_1m, return_3m, reached_25pct, reached_500pct,
                     time_to_mfe_min, time_to_mae_min)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    return_5m=EXCLUDED.return_5m, return_15m=EXCLUDED.return_15m,
                    return_30m=EXCLUDED.return_30m, return_60m=EXCLUDED.return_60m,
                    return_eod=EXCLUDED.return_eod, mfe_pct=EXCLUDED.mfe_pct,
                    mae_pct=EXCLUDED.mae_pct, reached_50pct=EXCLUDED.reached_50pct,
                    reached_100pct=EXCLUDED.reached_100pct,
                    reached_200pct=EXCLUDED.reached_200pct,
                    entry_success=EXCLUDED.entry_success,
                    strong_entry_success=EXCLUDED.strong_entry_success,
                    false_positive=EXCLUDED.false_positive,
                    contract_lod=EXCLUDED.contract_lod,
                    entry_vs_lod=EXCLUDED.entry_vs_lod,
                    pct_peak_captured=EXCLUDED.pct_peak_captured,
                    return_1m=EXCLUDED.return_1m, return_3m=EXCLUDED.return_3m,
                    reached_25pct=EXCLUDED.reached_25pct,
                    reached_500pct=EXCLUDED.reached_500pct,
                    time_to_mfe_min=EXCLUDED.time_to_mfe_min,
                    time_to_mae_min=EXCLUDED.time_to_mae_min,
                    created_at=NOW()
            """, outcome_rows)

        # ── counterfactual exits (§68) ──────────────────────────────────────────
        if cf_rows:
            cur.executemany("""
                INSERT INTO counterfactual_exits
                    (signal_id, session_date, strategy, return_pct,
                     capture_efficiency, diff_from_actual)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id, strategy) DO UPDATE SET
                    return_pct=EXCLUDED.return_pct,
                    capture_efficiency=EXCLUDED.capture_efficiency,
                    diff_from_actual=EXCLUDED.diff_from_actual
            """, cf_rows)

        # ── entry delay study (§69) ─────────────────────────────────────────────
        if delay_rows:
            cur.executemany("""
                INSERT INTO entry_delay_study
                    (signal_id, session_date, delay_min, delayed_entry_price,
                     mfe_pct, mae_pct, rule_pnl_pct, capture_efficiency,
                     move_missed_before_entry_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id, delay_min) DO UPDATE SET
                    delayed_entry_price=EXCLUDED.delayed_entry_price,
                    mfe_pct=EXCLUDED.mfe_pct, mae_pct=EXCLUDED.mae_pct,
                    rule_pnl_pct=EXCLUDED.rule_pnl_pct,
                    capture_efficiency=EXCLUDED.capture_efficiency,
                    move_missed_before_entry_pct=EXCLUDED.move_missed_before_entry_pct
            """, delay_rows)

        conn.commit()

        # ── missed opportunities (§72) — run after main commit ─────────────────
        try:
            missed = _scan_missed_opps(analysis_date, cur)
            if missed:
                _MCOLS = ('session_date', 'symbol', 'occ_symbol', 'strike', 'option_type',
                          'level_type', 'level_rank', 'event_start_time', 'local_low_price',
                          'maximum_price', 'maximum_return_pct', 'time_to_max_min',
                          'blocking_reason')
                cur.executemany(f"""
                    INSERT INTO missed_opportunities ({','.join(_MCOLS)})
                    VALUES ({','.join(['%s'] * len(_MCOLS))})
                """, [tuple(m[c] for c in _MCOLS) for m in missed])
                conn.commit()
                logger.info("Daily review %s: %d missed opportunities logged", analysis_date, len(missed))
        except Exception:
            logger.warning("Daily review: missed_opportunities scan failed", exc_info=True)
            conn.rollback()

        # ── §26-§29, §31: Signal volume analytics (post-session per signal) ────────
        try:
            # Pull all option_level_bars for the day once; group two ways:
            #   bars_by_contract[(sym, lt, rank, strike, otype)] = [{volume, bar_time}]
            #   all_sym_bars[sym]                                 = [{level_type, rank, ...}]
            cur.execute("""
                SELECT symbol, level_type, rank, strike::numeric, option_type,
                       bar_time, COALESCE(volume, 0)
                FROM option_level_bars WHERE level_date = %s ORDER BY bar_time
            """, (analysis_date,))
            bars_by_contract: dict = defaultdict(list)
            all_sym_bars:     dict = defaultdict(list)
            for sym2, lt, rnk, strk, otype, bt, vol in cur.fetchall():
                entry = {'level_type': lt, 'rank': int(rnk or 0),
                         'strike': float(strk), 'option_type': otype,
                         'volume': int(vol), 'bar_time': bt}
                bars_by_contract[(sym2, lt, int(rnk or 0), float(strk), otype)].append(entry)
                all_sym_bars[sym2].append(entry)

            # Resolve OI level ranks for each signal's level
            cur.execute("""
                SELECT o.symbol, o.level_type, o.rank, o.strike::numeric
                FROM oi_levels o WHERE o.level_date = %s
            """, (analysis_date,))
            rank_map: dict = {}
            for sym2, lt, rnk, strk in cur.fetchall():
                rank_map[(sym2, lt, float(strk))] = int(rnk)

            # Fetch signals with level context (may differ from already-iterated `sigs`)
            cur.execute("""
                SELECT id, symbol, signal_type, traded_strike::numeric,
                       option_type, level_type, level_price::numeric, trigger_price::numeric
                FROM signals WHERE signal_time::date = %s
            """, (analysis_date,))
            sva_rows: list[dict] = []
            for (sid2, sym2, styp2, tstrike2, otype2,
                 lvl_type, lvl_price, spot2) in cur.fetchall():
                if tstrike2 is None or otype2 is None:
                    continue
                tstrike2 = float(tstrike2)
                lt = lvl_type or ('SUPPORT' if styp2 == 'BULLISH' else 'RESISTANCE')
                lvl_price2 = float(lvl_price) if lvl_price else None
                rnk = (rank_map.get((sym2, lt, lvl_price2)) if lvl_price2 else None) or 1
                spot2 = float(spot2) if spot2 else 0.0

                contract_key = (sym2, lt, rnk, tstrike2, otype2)
                cbars = bars_by_contract.get(contract_key)
                if not cbars:
                    # Fallback: match by strike + option_type ignoring level metadata
                    cbars = [b for b in all_sym_bars.get(sym2, [])
                             if abs(b['strike'] - tstrike2) < 0.01
                             and b['option_type'] == otype2]
                if not cbars:
                    continue

                tf = multitf_volumes(cbars)
                sh = volume_shape_features(cbars)
                ne = norm_entropy(cbars)
                cr = chain_relative_volume(all_sym_bars.get(sym2, []), lt, rnk, spot2)
                mg = volume_migration(cbars, lt)

                sva_rows.append({
                    'signal_id': sid2, 'session_date': analysis_date, 'symbol': sym2,
                    **tf, **sh, 'normalized_entropy': ne, **cr, **mg,
                })

            if sva_rows:
                _SVA_COLS = (
                    'signal_id', 'session_date', 'symbol',
                    'vol_2m', 'vol_3m', 'vol_5m', 'vol_10m', 'vol_15m', 'vol_30m',
                    'ratio_2m', 'ratio_5m', 'ratio_10m', 'ratio_15m', 'ratio_30m',
                    'volume_shape', 'shape_hhi', 'burst_ratio', 'staircase_score',
                    'normalized_entropy',
                    'atm_vol_share', 'itm_vol_share', 'otm_vol_share',
                    'strike_volume_center', 'center_vs_spot',
                    'vol_center_change', 'vol_migration_direction',
                )
                execute_values(cur, f"""
                    INSERT INTO signal_volume_analytics ({','.join(_SVA_COLS)})
                    VALUES %s
                    ON CONFLICT (signal_id) DO UPDATE SET
                        vol_5m=EXCLUDED.vol_5m, vol_15m=EXCLUDED.vol_15m,
                        vol_30m=EXCLUDED.vol_30m, volume_shape=EXCLUDED.volume_shape,
                        shape_hhi=EXCLUDED.shape_hhi, burst_ratio=EXCLUDED.burst_ratio,
                        staircase_score=EXCLUDED.staircase_score,
                        normalized_entropy=EXCLUDED.normalized_entropy,
                        atm_vol_share=EXCLUDED.atm_vol_share,
                        itm_vol_share=EXCLUDED.itm_vol_share,
                        otm_vol_share=EXCLUDED.otm_vol_share,
                        strike_volume_center=EXCLUDED.strike_volume_center,
                        center_vs_spot=EXCLUDED.center_vs_spot,
                        vol_center_change=EXCLUDED.vol_center_change,
                        vol_migration_direction=EXCLUDED.vol_migration_direction
                """, [tuple(r.get(c) for c in _SVA_COLS) for r in sva_rows])
                conn.commit()
                logger.info("Daily review %s: %d signal_volume_analytics saved",
                            analysis_date, len(sva_rows))
        except Exception:
            logger.warning("Daily review: signal_volume_analytics failed", exc_info=True)
            conn.rollback()

        # ── §32: Historical strike-volume event archive ──────────────────────────
        try:
            cur.execute("""
                SELECT DISTINCT ON (symbol, strike, candidate_side)
                    symbol, ts AS event_time,
                    strike::numeric, candidate_side AS option_type,
                    atm_vol_1m, win_vol, active_bars,
                    alert_fired, contract_low_distance
                FROM signal_candidates
                WHERE session_date = %s AND valid_volume_event = TRUE
                ORDER BY symbol, strike, candidate_side, ts
            """, (analysis_date,))
            cands = cur.fetchall()

            # Clear any prior §32 rows for this date (safe on re-run)
            cur.execute("DELETE FROM volume_events WHERE session_date = %s", (analysis_date,))

            # Fetch expiry for occ_symbol construction
            cur.execute("""
                SELECT symbol, MIN(expiry_date) FROM option_chain_snapshots
                WHERE snap_date = %s GROUP BY symbol
            """, (analysis_date,))
            expiry_map2 = {s: e for s, e in cur.fetchall()}

            ve_rows: list[tuple] = []
            for (sym2, evt, strk, otype, tvol, wvol, abars, led, low_dist) in cands:
                strk = float(strk) if strk else None
                if strk is None or not otype:
                    continue

                # Bars for this contract on the day
                cur.execute("""
                    SELECT bar_time, high, low, close, COALESCE(volume, 0)
                    FROM option_level_bars
                    WHERE level_date = %s AND symbol = %s
                      AND ABS(strike::numeric - %s) < 0.01 AND option_type = %s
                    ORDER BY bar_time
                """, (analysis_date, sym2, strk, otype))
                cbars = cur.fetchall()
                if not cbars:
                    continue

                # Bars at/after event time for forward returns
                post = [(bt, float(h), float(l), float(c), int(v))
                        for bt, h, l, c, v in cbars if bt >= evt]
                if not post:
                    continue

                mark = post[0][3]
                hist_dicts = [{'volume': v} for *_, v in cbars]

                def _fwd_ret(mins):
                    cutoff = evt + timedelta(minutes=mins)
                    after = [b for b in post[1:] if b[0] >= cutoff]
                    if not after or mark <= 0:
                        return None
                    return round((after[0][3] / mark - 1) * 100, 2)

                post_highs = [b[1] for b in post[1:]]
                post_lows  = [b[2] for b in post[1:]]
                mfe2 = round((max(post_highs) / mark - 1) * 100, 2) if post_highs and mark > 0 else None
                mae2 = round((min(post_lows)  / mark - 1) * 100, 2) if post_lows  and mark > 0 else None

                # Event type from burst/cluster characteristics
                if abars is not None and abars <= 1:
                    evt_type = 'SINGLE_PRINT'
                elif (wvol and tvol and abars and abars >= 3 and wvol / max(int(tvol), 1) >= 0.60):
                    evt_type = 'STAIRCASE'
                else:
                    evt_type = 'CLUSTER'

                # Signal_id if this event led to a fired alert
                sig_id2 = None
                if led:
                    cur.execute("""
                        SELECT id FROM signals
                        WHERE symbol = %s AND signal_time::date = %s
                          AND ABS(traded_strike::numeric - %s) < 0.01
                          AND option_type = %s
                        LIMIT 1
                    """, (sym2, analysis_date, strk, otype))
                    sig_row = cur.fetchone()
                    if sig_row:
                        sig_id2 = sig_row[0]

                expiry2 = expiry_map2.get(sym2)
                occ2 = occ_symbol(sym2, expiry2, strk, otype) if expiry2 else None
                sh2 = volume_shape_features(hist_dicts)
                ne2 = norm_entropy(hist_dicts)

                ve_rows.append((
                    sym2, analysis_date, evt, occ2,
                    strk, otype, expiry2, evt_type,
                    int(tvol) if tvol else None, None,  # trigger_ratio = None (not stored)
                    round(mark, 4), float(low_dist) if low_dist else None,
                    sh2.get('volume_shape'), ne2,
                    bool(led), sig_id2,
                    _fwd_ret(5), _fwd_ret(15), _fwd_ret(30),
                    mfe2, mae2,
                ))

            if ve_rows:
                execute_values(cur, """
                    INSERT INTO volume_events
                        (symbol, session_date, event_time, occ_symbol,
                         strike, option_type, expiry, event_type,
                         trigger_volume, trigger_ratio, mark_at_event, low_dist,
                         volume_shape, normalized_entropy,
                         led_to_signal, signal_id,
                         return_5m, return_15m, return_30m, mfe_pct, mae_pct)
                    VALUES %s
                """, ve_rows)
                conn.commit()
                logger.info("Daily review %s: %d volume_events archived",
                            analysis_date, len(ve_rows))
        except Exception:
            logger.warning("Daily review: volume_events archive failed", exc_info=True)
            conn.rollback()

        # ── §34-§36: OI events (intent classification) + position lifecycle ─────────
        try:
            # Re-query volume_events (just written above) to get event rows with context
            cur.execute("""
                SELECT id, symbol, session_date, event_time, occ_symbol,
                       strike, option_type, expiry, trigger_volume,
                       mark_at_event, low_dist, volume_shape, event_type
                FROM volume_events WHERE session_date = %s
                ORDER BY symbol, strike, option_type, event_time
            """, (analysis_date,))
            ve_data = cur.fetchall()
            _VE_COLS = ('id','symbol','session_date','event_time','occ_symbol',
                        'strike','option_type','expiry','trigger_volume',
                        'mark_at_event','low_dist','volume_shape','event_type')
            ve_dicts = [dict(zip(_VE_COLS, row)) for row in ve_data]

            # For each event, compute session_high of the contract up to event_time
            # (so high_ratio = mark / session_high_at_event)
            cur.execute("""
                SELECT symbol, strike::numeric, option_type,
                       bar_time, MAX(close) OVER (
                           PARTITION BY symbol, strike, option_type
                           ORDER BY bar_time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS running_high
                FROM option_level_bars WHERE level_date = %s
                ORDER BY symbol, strike, option_type, bar_time
            """, (analysis_date,))
            # Build a lookup: (sym, strike, option_type) -> [(bar_time, running_high)]
            running_highs: dict = defaultdict(list)
            for sym2, strk, otype, bt, rh in cur.fetchall():
                running_highs[(sym2, float(strk), otype)].append((bt, float(rh) if rh else 0.0))

            def _get_session_high(sym2, strk, otype, event_time):
                entries = running_highs.get((sym2, float(strk) if strk else 0.0, otype), [])
                high = None
                for bt, rh in entries:
                    if bt <= event_time:
                        high = rh
                    else:
                        break
                return high

            # Clear prior oi_events and position_lifecycle for this date (idempotent re-run)
            cur.execute("DELETE FROM position_lifecycle WHERE session_date = %s", (analysis_date,))
            cur.execute("DELETE FROM oi_events WHERE session_date = %s", (analysis_date,))

            oi_event_insert_rows: list[tuple] = []
            for ev in ve_dicts:
                sym2  = ev['symbol']
                strk  = float(ev['strike']) if ev['strike'] else None
                otype = ev['option_type']
                evt   = ev['event_time']

                low_dist  = float(ev['low_dist']) if ev['low_dist'] is not None else None
                mark      = float(ev['mark_at_event']) if ev['mark_at_event'] else None
                sh        = _get_session_high(sym2, strk, otype, evt)
                high_ratio = round(mark / sh, 4) if (mark and sh and sh > 0) else None
                tod        = _tod_frac(evt)

                cls = classify_intent(
                    option_type     = otype or '',
                    low_dist        = low_dist,
                    high_ratio      = high_ratio,
                    volume_shape    = ev.get('volume_shape'),
                    event_type      = ev.get('event_type'),
                    time_of_day_frac = tod,
                )

                oi_event_insert_rows.append((
                    sym2, analysis_date, evt, ev.get('occ_symbol'),
                    strk, otype, ev.get('expiry'),
                    ev.get('trigger_volume'), mark, low_dist, high_ratio,
                    ev.get('volume_shape'), ev.get('event_type'), tod,
                    cls['live_intent'], cls['intent_probability'],
                    cls['intent_confidence'], cls['supporting_evidence'],
                    cls['contradicting_evidence'],
                ))

            if oi_event_insert_rows:
                execute_values(cur, """
                    INSERT INTO oi_events
                        (symbol, session_date, event_time, occ_symbol,
                         strike, option_type, expiry,
                         trigger_volume, mark_at_event, low_dist, high_ratio,
                         volume_shape, event_type, time_of_day_frac,
                         live_intent, intent_probability, intent_confidence,
                         supporting_evidence, contradicting_evidence)
                    VALUES %s
                    ON CONFLICT (symbol, session_date, event_time, strike, option_type)
                    DO UPDATE SET
                        live_intent=EXCLUDED.live_intent,
                        intent_probability=EXCLUDED.intent_probability,
                        intent_confidence=EXCLUDED.intent_confidence,
                        supporting_evidence=EXCLUDED.supporting_evidence,
                        contradicting_evidence=EXCLUDED.contradicting_evidence
                """, oi_event_insert_rows)
                conn.commit()

                # ── §36 Position lifecycle — pair open/close events ─────────────────
                # Re-read oi_events with the generated IDs + max/min prices
                cur.execute("""
                    SELECT oe.id, oe.symbol, oe.session_date, oe.event_time,
                           oe.occ_symbol, oe.strike, oe.option_type, oe.expiry,
                           oe.trigger_volume, oe.mark_at_event, oe.low_dist,
                           oe.high_ratio, oe.live_intent, oe.intent_probability,
                           COALESCE(stats.max_price, oe.mark_at_event) AS maximum_contract_price,
                           COALESCE(stats.min_price, oe.mark_at_event) AS minimum_contract_price
                    FROM oi_events oe
                    LEFT JOIN (
                        SELECT symbol, strike::numeric, option_type,
                               MAX(close) AS max_price, MIN(close) AS min_price
                        FROM option_level_bars WHERE level_date = %s
                        GROUP BY symbol, strike, option_type
                    ) stats ON stats.symbol = oe.symbol
                          AND stats.strike = oe.strike
                          AND stats.option_type = oe.option_type
                    WHERE oe.session_date = %s
                    ORDER BY oe.symbol, oe.strike, oe.option_type, oe.event_time
                """, (analysis_date, analysis_date))
                _LC_KEYS = ('id','symbol','session_date','event_time','occ_symbol',
                            'strike','option_type','expiry','trigger_volume','mark_at_event',
                            'low_dist','high_ratio','live_intent','intent_probability',
                            'maximum_contract_price','minimum_contract_price')
                oe_full = [dict(zip(_LC_KEYS, row)) for row in cur.fetchall()]

                lifecycles = compute_lifecycle_pairs(oe_full)
                if lifecycles:
                    _LC_COLS = (
                        'symbol','session_date','occ_symbol','strike','option_type','expiry',
                        'open_event_id','probable_open_time','probable_open_price',
                        'probable_open_volume','probable_position_type','opening_probability',
                        'maximum_contract_price','minimum_contract_price',
                        'close_event_id','probable_close_time','probable_close_price',
                        'probable_close_volume','closing_probability',
                        'confirmed_oi_change','lifecycle_return_pct','confidence',
                    )
                    execute_values(cur, f"""
                        INSERT INTO position_lifecycle ({','.join(_LC_COLS)})
                        VALUES %s
                        ON CONFLICT (symbol, session_date, strike, option_type) DO UPDATE SET
                            open_event_id=EXCLUDED.open_event_id,
                            probable_open_time=EXCLUDED.probable_open_time,
                            probable_open_price=EXCLUDED.probable_open_price,
                            probable_position_type=EXCLUDED.probable_position_type,
                            opening_probability=EXCLUDED.opening_probability,
                            close_event_id=EXCLUDED.close_event_id,
                            probable_close_time=EXCLUDED.probable_close_time,
                            probable_close_price=EXCLUDED.probable_close_price,
                            closing_probability=EXCLUDED.closing_probability,
                            lifecycle_return_pct=EXCLUDED.lifecycle_return_pct,
                            confidence=EXCLUDED.confidence
                    """, [tuple(lc.get(c) for c in _LC_COLS) for lc in lifecycles])
                    conn.commit()

                logger.info("Daily review %s: %d oi_events classified, %d lifecycles built",
                            analysis_date, len(oi_event_insert_rows),
                            len(lifecycles) if lifecycles else 0)
        except Exception:
            logger.warning("Daily review: oi_events/lifecycle failed", exc_info=True)
            conn.rollback()

        logger.info("Daily review %s: %d signals -> signal_analysis, %d outcome labels, "
                    "%d counterfactuals, %d delay rows",
                    analysis_date, len(rows), len(outcome_rows), len(cf_rows), len(delay_rows))

        # ── Phase 6 §74: Permutation test — signal events vs random controls ──────
        # Compare return_30m for events that led to a signal vs events that did not.
        try:
            from analysis.research_toolkit import permutation_test as _perm_test
            import json as _json

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT return_30m, led_to_signal
                    FROM   volume_events
                    WHERE  session_date = %s
                      AND  return_30m  IS NOT NULL
                """, (analysis_date,))
                ve_ret_rows = cur.fetchall()

            sig_rets  = [float(r) for r, led in ve_ret_rows if led]
            ctrl_rets = [float(r) for r, led in ve_ret_rows if not led]

            if len(sig_rets) >= 3 and len(ctrl_rets) >= 3:
                perm_result = _perm_test(sig_rets, ctrl_rets, metric='mean')
                if perm_result:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO research_permutation_tests
                                (session_date, symbol, test_name, metric,
                                 n_observed, n_control, n_permutations,
                                 observed_metric, null_mean, null_std,
                                 p_value, effect_size, percentile_rank,
                                 ci_lower, ci_upper, significant)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (
                            analysis_date, None,
                            'signal_event_30m_return_vs_random', perm_result.get('metric'),
                            perm_result.get('n_observed'), perm_result.get('n_control'),
                            perm_result.get('n_permutations'),
                            perm_result.get('observed_metric'), perm_result.get('null_mean'),
                            perm_result.get('null_std'), perm_result.get('p_value'),
                            perm_result.get('effect_size'), perm_result.get('percentile_rank'),
                            perm_result.get('ci_lower'), perm_result.get('ci_upper'),
                            perm_result.get('significant'),
                        ))
                    conn.commit()
                    logger.info(
                        "Daily review %s: permutation test p=%.4f effect_size=%.3f significant=%s",
                        analysis_date, perm_result.get('p_value', 1.0),
                        perm_result.get('effect_size', 0.0),
                        perm_result.get('significant'),
                    )
        except Exception:
            logger.warning("Daily review: permutation test failed", exc_info=True)
            conn.rollback()

        # ── Phase 6 §75: Monte Carlo — simulate trade sequences ─────────────────
        # Pull rule_pnl_pct from signal_analysis (all available sessions, up to 180d).
        try:
            from analysis.research_toolkit import monte_carlo_trades as _mc_trades

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT rule_pnl_pct
                    FROM   signal_analysis
                    WHERE  rule_pnl_pct IS NOT NULL
                      AND  analysis_date >= (CURRENT_DATE - INTERVAL '180 days')
                    ORDER  BY analysis_date
                """)
                pnl_rows = cur.fetchall()

            trade_rets = [float(r[0]) / 100.0 for r in pnl_rows if r[0] is not None]

            if len(trade_rets) >= 5:
                mc_result = _mc_trades(trade_rets)
                if mc_result:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO research_monte_carlo
                                (session_date, symbol,
                                 n_trades, n_simulations, starting_capital,
                                 expected_return, median_return,
                                 probability_of_loss, probability_of_ruin,
                                 target_hit_probability,
                                 max_drawdown_p5, max_drawdown_p50, max_drawdown_p95,
                                 ci_lower_95, ci_upper_95)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (
                            analysis_date, None,
                            mc_result.get('n_trades'), mc_result.get('n_simulations'),
                            mc_result.get('starting_capital'),
                            mc_result.get('expected_return'), mc_result.get('median_return'),
                            mc_result.get('probability_of_loss'),
                            mc_result.get('probability_of_ruin'),
                            mc_result.get('target_hit_probability'),
                            mc_result.get('max_drawdown_p5'), mc_result.get('max_drawdown_p50'),
                            mc_result.get('max_drawdown_p95'),
                            mc_result.get('ci_lower_95'), mc_result.get('ci_upper_95'),
                        ))
                    conn.commit()
                    logger.info(
                        "Daily review %s: MC %d trades  E[ret]=%.2f%%  P(ruin)=%.3f  "
                        "P(loss)=%.3f  MDD_p50=%.2f%%",
                        analysis_date, mc_result.get('n_trades', 0),
                        (mc_result.get('expected_return') or 0) * 100,
                        mc_result.get('probability_of_ruin', 0),
                        mc_result.get('probability_of_loss', 0),
                        (mc_result.get('max_drawdown_p50') or 0) * 100,
                    )
        except Exception:
            logger.warning("Daily review: Monte Carlo failed", exc_info=True)
            conn.rollback()

        # ── Phase 6 §77: Change-point detection on per-minute volume ────────────
        # Uses volume_leadership (call_vol_5m, put_vol_5m) for today, per symbol.
        try:
            from analysis.research_toolkit import detect_volume_change_points as _cpd
            import json as _json2

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT symbol, call_vol_5m, put_vol_5m
                    FROM   volume_leadership
                    WHERE  session_date = %s
                    ORDER  BY symbol, bar_time
                """, (analysis_date,))
                vl_rows = cur.fetchall()

            # Build per-symbol call/put series
            sym_call: dict[str, list] = defaultdict(list)
            sym_put:  dict[str, list] = defaultdict(list)
            for sym, cv, pv in vl_rows:
                if cv is not None: sym_call[sym].append(float(cv))
                if pv is not None: sym_put[sym].append(float(pv))

            cp_insert: list[tuple] = []
            for sym in set(sym_call) | set(sym_put):
                for side, series in (('CALL', sym_call.get(sym, [])),
                                     ('PUT',  sym_put.get(sym, []))):
                    if len(series) < 6:
                        continue
                    r = _cpd(series, model='rbf', pen=3.0, min_size=3)
                    if not r:
                        continue
                    r['n_bars'] = len(series)
                    cp_insert.append((
                        analysis_date, sym, side,
                        r.get('n_bars'), r.get('n_breakpoints'),
                        _json2.dumps(r.get('breakpoint_indices', [])),
                        r.get('pre_regime_mean'), r.get('post_regime_mean'),
                        r.get('regime_change_ratio'),
                        r.get('concentrated_event_detected'),
                        r.get('model_used'), 3.0,
                    ))

            if cp_insert:
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO research_change_points
                            (session_date, symbol, option_side,
                             n_bars, n_breakpoints, breakpoint_indices,
                             pre_regime_mean, post_regime_mean, regime_change_ratio,
                             concentrated_event_detected, model_used, pen)
                        VALUES %s
                    """, cp_insert)
                conn.commit()
                concentrated = sum(1 for r in cp_insert if r[9])
                logger.info(
                    "Daily review %s: change-point detection — %d series, "
                    "%d concentrated-event regimes detected",
                    analysis_date, len(cp_insert), concentrated,
                )
        except Exception:
            logger.warning("Daily review: change-point detection failed", exc_info=True)
            conn.rollback()

        # ── Deliver the recommendations (Discord + Google Sheets) ───────────────
        try:
            send_daily_review(rows, analysis_date)
        except Exception:
            logger.warning("Daily review: Discord delivery failed", exc_info=True)
        if sheets is not None:
            try:
                sheets.log_daily_review(rows)
            except Exception:
                logger.warning("Daily review: Sheets logging failed", exc_info=True)
        return len(rows)
    finally:
        conn.close()

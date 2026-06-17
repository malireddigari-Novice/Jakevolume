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
from datetime import date as _date, timedelta

import psycopg2
import config
from analysis.signal_detector import compute_exit_targets
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
    Objective outcome labels (spec §20-§24) from the traded contract's path after entry.
    opath: [(bar_time, high, low, close), ...] starting at the entry bar. Python computes
    these — not the label, the raw outcomes — so labels can be redefined later.
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
    return dict(
        entry_price=round(entry, 4),
        return_5m=ret_at(5), return_15m=ret_at(15), return_30m=ret_at(30),
        return_60m=ret_at(60), return_eod=round((float(opath[-1][3]) / entry - 1) * 100, 2),
        mfe_pct=round(mfe, 2), mae_pct=round(mae, 2),
        reached_50pct=mfe >= 50, reached_100pct=mfe >= 100, reached_200pct=mfe >= 200,
        entry_success=(first_touch(0.50, 0.35, 30) == 'up'),
        strong_entry_success=(first_touch(1.00, 0.35, 60) == 'up'),
        false_positive=(first_touch(0.25, 0.35, 30) == 'down'),
    )


# ── main entry point ───────────────────────────────────────────────────────────

def analyze_daily_signals(analysis_date: _date, data_src=None, sheets=None) -> int:
    """Analyze all of analysis_date's signals -> signal_analysis, then deliver the
    recommendations to Discord (and Google Sheets if `sheets` is given). Returns rows written."""
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

        rows = []
        outcome_rows = []
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
            peak  = max(highs) if highs else entry
            trough = min(lows) if lows else entry
            peak_i = ei + 1 + highs.index(peak) if highs else ei
            peak_time = ob[peak_i][0]
            mfe = (peak / entry - 1) * 100
            mae = (trough / entry - 1) * 100

            # underlying path + current-rule outcome
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

            # Objective outcome labels (§20-§24) + trade-quality metrics
            lab = _outcome_labels(st, opath)
            # Low UP TO entry (not the whole day — 0DTE decays to ~0, which would be
            # meaningless): how chased the entry was vs the best price available then.
            contract_lod = min((float(b[2]) for b in ob[:ei + 1]), default=entry)
            entry_vs_lod = round(entry / max(contract_lod, 0.01), 3)        # 1.0 = bought the low
            # % of the peak move the current rule captured — only meaningful when a real
            # peak existed (>=25%); negative = we lost while a move was there.
            pct_peak = round(rule_pnl / mfe * 100, 1) if mfe >= 25 else None
            outcome_rows.append((sid, analysis_date, sym, lab['entry_price'],
                                 lab['return_5m'], lab['return_15m'], lab['return_30m'],
                                 lab['return_60m'], lab['return_eod'], lab['mfe_pct'], lab['mae_pct'],
                                 lab['reached_50pct'], lab['reached_100pct'], lab['reached_200pct'],
                                 lab['entry_success'], lab['strong_entry_success'], lab['false_positive'],
                                 round(contract_lod, 4), entry_vs_lod, pct_peak))

            action, sug_pnl, text = _suggest(entry, opath)
            rows.append(dict(signal_id=sid, analysis_date=analysis_date, symbol=sym,
                             signal_time=st, signal_type=styp, traded_strike=tstrike,
                             option_type=otype, entry_price=round(entry, 4),
                             mfe_pct=round(mfe, 2), mae_pct=round(mae, 2), peak_price=round(peak, 4),
                             peak_time=peak_time, trough_price=round(trough, 4),
                             rule_pnl_pct=round(rule_pnl, 2), suggested_action=action,
                             suggested_pnl_pct=round(sug_pnl, 2), suggestion=text,
                             data_source=source))

        _COLS = ('signal_id', 'analysis_date', 'symbol', 'signal_time', 'signal_type',
                 'traded_strike', 'option_type', 'entry_price', 'mfe_pct', 'mae_pct',
                 'peak_price', 'peak_time', 'trough_price', 'rule_pnl_pct', 'suggested_action',
                 'suggested_pnl_pct', 'suggestion', 'data_source')
        sql = """
            INSERT INTO signal_analysis
                (signal_id, analysis_date, symbol, signal_time, signal_type, traded_strike,
                 option_type, entry_price, mfe_pct, mae_pct, peak_price, peak_time, trough_price,
                 rule_pnl_pct, suggested_action, suggested_pnl_pct, suggestion, data_source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (signal_id) DO UPDATE SET
                analysis_date=EXCLUDED.analysis_date, entry_price=EXCLUDED.entry_price,
                mfe_pct=EXCLUDED.mfe_pct, mae_pct=EXCLUDED.mae_pct, peak_price=EXCLUDED.peak_price,
                peak_time=EXCLUDED.peak_time, trough_price=EXCLUDED.trough_price,
                rule_pnl_pct=EXCLUDED.rule_pnl_pct, suggested_action=EXCLUDED.suggested_action,
                suggested_pnl_pct=EXCLUDED.suggested_pnl_pct, suggestion=EXCLUDED.suggestion,
                data_source=EXCLUDED.data_source, created_at=NOW()
        """
        cur.executemany(sql, [tuple(r[c] for c in _COLS) for r in rows])

        # Objective outcome labels (§20-§24) -> signal_outcomes
        if outcome_rows:
            cur.executemany("""
                INSERT INTO signal_outcomes
                    (signal_id, session_date, symbol, entry_price, return_5m, return_15m,
                     return_30m, return_60m, return_eod, mfe_pct, mae_pct, reached_50pct,
                     reached_100pct, reached_200pct, entry_success, strong_entry_success,
                     false_positive, contract_lod, entry_vs_lod, pct_peak_captured)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    return_5m=EXCLUDED.return_5m, return_15m=EXCLUDED.return_15m,
                    return_30m=EXCLUDED.return_30m, return_60m=EXCLUDED.return_60m,
                    return_eod=EXCLUDED.return_eod, mfe_pct=EXCLUDED.mfe_pct, mae_pct=EXCLUDED.mae_pct,
                    reached_50pct=EXCLUDED.reached_50pct, reached_100pct=EXCLUDED.reached_100pct,
                    reached_200pct=EXCLUDED.reached_200pct, entry_success=EXCLUDED.entry_success,
                    strong_entry_success=EXCLUDED.strong_entry_success,
                    false_positive=EXCLUDED.false_positive, contract_lod=EXCLUDED.contract_lod,
                    entry_vs_lod=EXCLUDED.entry_vs_lod, pct_peak_captured=EXCLUDED.pct_peak_captured,
                    created_at=NOW()
            """, outcome_rows)
        conn.commit()
        logger.info("Daily review %s: analyzed %d signals -> signal_analysis (+%d outcome labels)",
                    analysis_date, len(rows), len(outcome_rows))

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

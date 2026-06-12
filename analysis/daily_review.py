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
from datetime import date as _date

import psycopg2
import config
from analysis.signal_detector import compute_exit_targets
from data.alpaca_client import occ_symbol
from data.market_utils import CST

logger = logging.getLogger(__name__)


def _conn():
    return psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD)


# ── exit / suggestion simulators on the option price path ──────────────────────

def _current_rule(styp, entry, opath, ubymin, e1, e2):
    """Current live exit: half at e1 (underlying), rest at e2, -50% stop, BE after e1, EOD."""
    stop, held, proc, e1done = 0.5 * entry, 1.0, 0.0, False
    for (t, h, l, c) in opath[1:]:
        if held > 0 and l <= stop:
            proc += held * stop; held = 0.0; break
        u = ubymin.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            def hit(x): return x is not None and ((uh >= x) if styp == 'BULLISH' else (ul <= x))
            if not e1done and hit(e1):
                proc += 0.5 * c; held -= 0.5; e1done = True; stop = entry
            if e1done and held > 0 and hit(e2):
                proc += held * c; held = 0.0; break
    if held > 0:
        proc += held * opath[-1][3]
    return (proc - entry) / entry * 100


def _ladder(entry, opath, legs, stop_pct=0.50, trail_arm=None, trail_pct=None):
    """Scale out at option-price take-profit legs [(gain_pct, qty_frac)]; remainder trails
    (if set) or rides to EOD; -50% hard stop on whatever is still held."""
    stop, held, proc, peak = entry * (1 - stop_pct), 1.0, 0.0, entry
    legs = sorted(legs); li = 0
    for (t, h, l, c) in opath[1:]:
        if held > 0 and l <= stop:
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
        pnl = _ladder(entry, opath, [(0.15, 1.0)])      # scalp ~+15% if reached, else stop/EOD
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


# ── main entry point ───────────────────────────────────────────────────────────

def analyze_daily_signals(analysis_date: _date, data_src=None) -> int:
    """Analyze all of analysis_date's signals → signal_analysis. Returns rows written."""
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
                rows.append((sid, analysis_date, sym, st, styp, tstrike, otype, None,
                             None, None, None, None, None, None, 'NO_DATA', None,
                             'No intraday price path available for the traded contract.',
                             source))
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

            action, sug_pnl, text = _suggest(entry, opath)
            rows.append((sid, analysis_date, sym, st, styp, tstrike, otype, round(entry, 4),
                         round(mfe, 2), round(mae, 2), round(peak, 4), peak_time, round(trough, 4),
                         round(rule_pnl, 2), action, round(sug_pnl, 2), text, source))

        cur.execute("""CREATE TABLE IF NOT EXISTS signal_analysis (id BIGSERIAL PRIMARY KEY)""")  # no-op safety
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
        cur.executemany(sql, rows)
        conn.commit()
        logger.info("Daily review %s: analyzed %d signals → signal_analysis", analysis_date, len(rows))
        return len(rows)
    finally:
        conn.close()

"""
Shadow comparison: OLD 500/1000 volume gate vs NEW 1000/2000 (opening 1250/2500).

READ-ONLY. Replays every fired signal that has an objective outcome and asks: which
alerts would the tightened floor REMOVE, and were they winners or losers? Reports the
change in expectancy, paper-equity curve, and max drawdown.

Usage:  python shadow_gate_comparison.py [YYYY-MM-DD_from] [YYYY-MM-DD_to]

Gate = pass at least one:  peak_1m >= floor_1m  OR  vol_3m >= floor_3m
  OLD:      500 / 1000  (flat)
  NEW:     1000 / 2000  (1250 / 2500 during the first 15 min after the open)
peak_1m/vol_3m are approximated by the stored atm_vol_1m / atm_vol_3m on each signal.
P&L per trade uses signal_outcomes.return_30m; winner/loser from entry_success/false_positive
(falling back to the sign of return_30m).
"""
import sys
from datetime import time as dtime

import config
import db.ops as db

OLD_1M, OLD_3M = 500, 1000
NEW_1M, NEW_3M = config.PEAK_1M_VOLUME_MIN, config.VOLUME_3M_MIN          # 1000 / 2000
OPEN_1M, OPEN_3M = config.OPENING_PEAK_1M_VOLUME_MIN, config.OPENING_VOLUME_3M_MIN  # 1250 / 2500
# Opening window = first 15 min after the 08:30 CST cash open.
_OPEN_START = dtime(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)
_OPEN_END   = dtime(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE + 15)


def _passes(v1, v3, f1, f3):
    return (v1 or 0) >= f1 or (v3 or 0) >= f3


def _max_drawdown(pnls):
    """Max peak-to-trough drop of the cumulative equity curve."""
    eq = peak = 0.0
    mdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return eq, mdd


def main(d_from, d_to):
    db.init_pool()
    conn = db._get()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.signal_time, s.symbol, s.signal_type, s.atm_vol_1m, s.atm_vol_3m,
                   o.return_30m, o.entry_success, o.false_positive
            FROM   signals s
            JOIN   signal_outcomes o ON o.signal_id = s.id
            WHERE  o.session_date BETWEEN %s AND %s
            ORDER  BY s.signal_time
        """, (d_from, d_to))
        rows = cur.fetchall()
    db._put(conn)

    if not rows:
        print(f"No fired signals with outcomes between {d_from} and {d_to}.")
        return 0

    old_pnls, new_pnls = [], []
    removed = []
    for st, sym, styp, v1, v3, r30, win, fp in rows:
        opening = _OPEN_START <= st.time() < _OPEN_END
        f1, f3 = (OPEN_1M, OPEN_3M) if opening else (NEW_1M, NEW_3M)
        old_ok = _passes(v1, v3, OLD_1M, OLD_3M)
        new_ok = _passes(v1, v3, f1, f3)
        pnl = float(r30) if r30 is not None else 0.0
        is_win = bool(win) if win is not None else (pnl > 0)
        if old_ok:
            old_pnls.append(pnl)
        if new_ok:
            new_pnls.append(pnl)
        elif old_ok:                                  # fired under old, dropped by new
            removed.append((st, sym, styp, v1 or 0, v3 or 0, pnl, is_win, opening))

    win_removed  = sum(1 for r in removed if r[6])
    loss_removed = len(removed) - win_removed
    old_eq, old_mdd = _max_drawdown(old_pnls)
    new_eq, new_mdd = _max_drawdown(new_pnls)
    old_exp = (sum(old_pnls) / len(old_pnls)) if old_pnls else 0.0
    new_exp = (sum(new_pnls) / len(new_pnls)) if new_pnls else 0.0

    print(f"\n=== Shadow gate comparison  {d_from} → {d_to} ===")
    print(f"OLD gate 500/1000   |  NEW gate {NEW_1M}/{NEW_3M} (opening {OPEN_1M}/{OPEN_3M})\n")
    print(f"  alerts (old → new) : {len(old_pnls)} → {len(new_pnls)}  "
          f"(removed {len(removed)})")
    print(f"  winners removed    : {win_removed}")
    print(f"  losers removed     : {loss_removed}")
    print(f"  expectancy/trade   : {old_exp:+.1f}% → {new_exp:+.1f}%  "
          f"(Δ {new_exp - old_exp:+.1f}%)   [return_30m]")
    print(f"  final paper equity : {old_eq:+.1f}% → {new_eq:+.1f}%  (Δ {new_eq - old_eq:+.1f}%)")
    print(f"  max drawdown       : {old_mdd:+.1f}% → {new_mdd:+.1f}%  (Δ {new_mdd - old_mdd:+.1f}%)")

    if removed:
        print("\n  Removed alerts (fired under old, blocked by new):")
        for st, sym, styp, v1, v3, pnl, is_win, opening in removed:
            tag = 'WIN ' if is_win else 'LOSS'
            op = ' [opening]' if opening else ''
            print(f"    {st:%m-%d %H:%M} {sym:<5} {styp:<8} 1m={v1:<5} 3m={v3:<6} "
                  f"→ {pnl:+6.1f}%  {tag}{op}")
    print()
    return 0


if __name__ == "__main__":
    frm = sys.argv[1] if len(sys.argv) > 1 else '2026-01-01'
    to  = sys.argv[2] if len(sys.argv) > 2 else '2026-12-31'
    raise SystemExit(main(frm, to))

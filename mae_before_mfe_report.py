"""
MAE-before-MFE report (P5, §11) — read-only.

A trade whose peak (MFE) only came AFTER a severe drawdown (MAE) is a poor 0DTE entry
even if it eventually printed green (the MSFT-385P / AMZN lessons: correct-eventually but
-85%/-92% first). This surfaces, per fired signal with an outcome, whether the adverse
move preceded the favorable one and how deep it was — so early drawdown is penalized, not
hidden by the eventual peak.

Usage:  python mae_before_mfe_report.py [YYYY-MM-DD_from] [YYYY-MM-DD_to]
"""
import sys

import db.ops as db

SEVERE_DD = 50.0   # |MAE| >= this (%) that occurred before MFE = a severe early drawdown


def main(d_from, d_to):
    db.init_pool()
    conn = db._get()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.signal_time, s.symbol, s.signal_type, s.gold_subtype,
                   o.mfe_pct, o.mae_pct, o.time_to_mfe_min, o.time_to_mae_min,
                   o.entry_success, o.false_positive
            FROM   signals s JOIN signal_outcomes o ON o.signal_id = s.id
            WHERE  o.session_date BETWEEN %s AND %s
            ORDER  BY s.signal_time
        """, (d_from, d_to))
        rows = cur.fetchall()
    db._put(conn)

    if not rows:
        print(f"No fired signals with outcomes between {d_from} and {d_to}.")
        return 0

    total = len(rows)
    mae_first = severe = 0
    print(f"\n=== MAE-before-MFE  {d_from} → {d_to}  ({total} trades) ===\n")
    for st, sym, styp, sub, mfe, mae, t_mfe, t_mae, win, fp in rows:
        mfe, mae = float(mfe or 0), float(mae or 0)
        tm, ta = (t_mfe if t_mfe is not None else 9999), (t_mae if t_mae is not None else 9999)
        before = ta <= tm and mae < 0
        sev = before and abs(mae) >= SEVERE_DD
        mae_first += 1 if before else 0
        severe += 1 if sev else 0
        tag = 'WIN ' if win else ('LOSS' if fp else '—')
        flag = '  ⚠ SEVERE-EARLY-DD' if sev else ('  (mae-before-mfe)' if before else '')
        print(f"  {st:%m-%d %H:%M} {sym:<5} {styp:<8} {tag}  "
              f"MFE {mfe:+6.1f}% @{t_mfe}m  MAE {mae:+6.1f}% @{t_mae}m{flag}")

    print(f"\n  mae-before-mfe: {mae_first}/{total} ({100*mae_first/total:.0f}%)")
    print(f"  severe early drawdown (|MAE|>= {SEVERE_DD:.0f}% before MFE): "
          f"{severe}/{total} ({100*severe/total:.0f}%)")
    print("  -> production quality should penalize the severe-early-dd trades even when they")
    print("     eventually printed green (correct direction, premature timing).\n")
    return 0


if __name__ == "__main__":
    frm = sys.argv[1] if len(sys.argv) > 1 else '2026-01-01'
    to  = sys.argv[2] if len(sys.argv) > 2 else '2026-12-31'
    raise SystemExit(main(frm, to))

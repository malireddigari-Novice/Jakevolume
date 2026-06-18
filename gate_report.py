"""
End-of-day production-volume-gate summary (Step 1).

Per symbol: candidate setups evaluated, Path A / Path B passes, gold-standard alerts,
pending partial-bar candidates (and how many later passed / failed), the main block
buckets, and the actual production alerts. Then a per-alert detail line.

A "setup" is a distinct (symbol, strike, side) for the session — candidates are logged
every poll, so we aggregate poll-rows into setups (bool_or over the day).

Usage:  python gate_report.py [YYYY-MM-DD]   (default: today)
Importable: build_gate_report(session_date) -> str
"""
import sys
from datetime import date as _date

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
import psycopg2

_SETUPS = """
WITH setups AS (
    SELECT symbol, strike, candidate_side,
           bool_or(gate_path = 'A')  AS path_a,
           bool_or(gate_path = 'B')  AS path_b,
           bool_or(gold_standard)    AS gold,
           bool_or(pending)          AS pending,
           bool_or(alert_fired)      AS fired,
           (array_agg(blocked_reason ORDER BY ts DESC))[1] AS last_reason
    FROM   signal_candidates
    WHERE  session_date = %s
    GROUP  BY symbol, strike, candidate_side
)
SELECT symbol,
       COUNT(*)                                                          AS total,
       COUNT(*) FILTER (WHERE path_a)                                    AS path_a,
       COUNT(*) FILTER (WHERE path_b)                                    AS path_b,
       COUNT(*) FILTER (WHERE gold)                                      AS gold,
       COUNT(*) FILTER (WHERE pending)                                   AS pending,
       COUNT(*) FILTER (WHERE pending AND fired)                         AS pend_pass,
       COUNT(*) FILTER (WHERE pending AND NOT fired)                     AS pend_fail,
       COUNT(*) FILTER (WHERE NOT fired AND last_reason LIKE '%%INSUFFICIENT_CONVICTION%%') AS insufficient,
       COUNT(*) FILTER (WHERE NOT fired AND last_reason LIKE '%%LOW_PREMIUM_NOTIONAL%%')    AS low_notional,
       COUNT(*) FILTER (WHERE NOT fired AND last_reason LIKE '%%LOW_EVENT_SHARE%%')         AS low_share,
       COUNT(*) FILTER (WHERE NOT fired AND last_reason LIKE '%%PERSISTENT_BACKGROUND%%')   AS persist_bg,
       COUNT(*) FILTER (WHERE fired)                                     AS alerts
FROM   setups
GROUP  BY symbol
ORDER  BY symbol
"""

_ALERTS = """
SELECT symbol, candidate_side, level_label, strike, gate_path, gold_standard,
       trigger_volume, observed_vol, completed_vol, trigger_ratio, event_share,
       premium_notional, contract_low_distance, bar_status, ts::time(0)
FROM   signal_candidates
WHERE  session_date = %s AND alert_fired = TRUE
ORDER  BY ts
"""


def build_gate_report(session_date) -> str:
    c = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                         user=config.DB_USER, password=config.DB_PASSWORD)
    cur = c.cursor()
    cur.execute(_SETUPS, (session_date,))
    rows = cur.fetchall()
    cur.execute(_ALERTS, (session_date,))
    alerts = cur.fetchall()
    c.close()

    out = [f"PRODUCTION VOLUME-GATE REPORT — {session_date}", "=" * 96]
    if not rows:
        out.append("(no candidate setups recorded)")
        return "\n".join(out)

    hdr = (f"{'SYM':6}{'setups':>7}{'A':>4}{'B':>4}{'gold':>6}{'pend':>6}{'p→ok':>6}"
           f"{'p→x':>5}{'insuf':>7}{'notnl':>7}{'shr':>5}{'bg':>4}{'ALERTS':>8}")
    out += [hdr, "-" * len(hdr)]
    tot = [0] * 12
    for r in rows:
        sym, t, a, b, g, pend, pok, pf, insf, notl, shr, bg, al = r
        vals = [t, a, b, g, pend, pok, pf, insf, notl, shr, bg, al]
        tot = [tot[i] + vals[i] for i in range(12)]
        out.append(f"{sym:6}{t:>7}{a:>4}{b:>4}{g:>6}{pend:>6}{pok:>6}{pf:>5}"
                   f"{insf:>7}{notl:>7}{shr:>5}{bg:>4}{al:>8}")
    out.append("-" * len(hdr))
    out.append(f"{'TOTAL':6}{tot[0]:>7}{tot[1]:>4}{tot[2]:>4}{tot[3]:>6}{tot[4]:>6}{tot[5]:>6}"
               f"{tot[6]:>5}{tot[7]:>7}{tot[8]:>7}{tot[9]:>5}{tot[10]:>4}{tot[11]:>8}")
    out.append("  cols: A/B=Path passes · gold=GoldStandard · pend=pending partial-bar · "
               "p→ok/p→x=pending later passed/failed · insuf/notnl/shr/bg=blocks")

    out += ["", f"PRODUCTION ALERTS ({len(alerts)})", "=" * 96]
    if not alerts:
        out.append("(none)")
    for a in alerts:
        (sym, side, lbl, strike, path, gold, trigv, obsv, compv, ratio, share,
         notional, lowd, barstat, t) = a
        gflag = "  ★GOLD" if gold else ""
        out.append(
            f"{t}  {sym} {float(strike):g}{side[0]} @{lbl}  Path {path}{gflag}\n"
            f"        trigVol={trigv}  observed={obsv}  completed={compv}  bar={barstat}\n"
            f"        ratio={ratio}  EventShare={share}  notional=${(notional or 0):,.0f}  "
            f"ContractLowDist={lowd}  Gold={bool(gold)}"
        )
    return "\n".join(out)


if __name__ == "__main__":
    d = _date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    if d is None:
        c = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                             user=config.DB_USER, password=config.DB_PASSWORD)
        cur = c.cursor(); cur.execute("SELECT CURRENT_DATE"); d = cur.fetchone()[0]; c.close()
    print(build_gate_report(d))

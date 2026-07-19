"""
candidate_forensics.py — "Why did (or didn't) this contract alert?"

Turns a missed-alert investigation from detective work into one command. Given a
contract and date, it reconstructs the per-minute gate trace from the data the engine
already logs (`signal_candidates`), joins symbol leadership (`volume_leadership`), and
falls back to the coverage log (`candidate_coverage`) for off-level strikes the level
path never evaluated — then prints the exact gate that rejected it, with the values at
the time, in the pipeline order the detector applies them.

Usage:
  python candidate_forensics.py NVDA 200 CALL 2026-07-09
  python candidate_forensics.py META 635 C 2026-07-17 --all-minutes
"""
import argparse
import sys

import psycopg2

import config

# Primary-level gate pipeline, in the order signal_detector.check() applies them.
# (name, predicate over the blocked_reason). A row's reason means every gate BEFORE
# it PASSED that minute (gates short-circuit), the matching gate FAILED, and gates
# after it were not evaluated.
GATES = [
    ('Location (near level)',      lambda r: r in ('NOT_NEAR_LEVEL', 'FALSE_BREAKOUT_OR_BREAKDOWN', 'NO_QUOTES')),
    ('Contract Low / chased (§12)', lambda r: r == 'CONTRACT_CHASED'),
    ('Volume',                     lambda r: r.startswith('NO_VALID_VOLUME_SIGNAL')),
    ('Historical Value (§13)',     lambda r: r == 'HISTORICAL_VALUE_TOO_HIGH'),
    ('Short-cover (§14)',          lambda r: r == 'SHORT_COVER_RISK'),
    ('Already alerted (§19)',      lambda r: r == 'ALREADY_ALERTED_TODAY'),
    ('Countertrend / chain',       lambda r: r.startswith('COUNTERTREND')),
]


def _conn():
    return psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD, connect_timeout=8)


def _fail_index(reason: str):
    """Index of the gate that failed for this blocked_reason, or None if PASSED."""
    if reason == 'PASSED':
        return None
    for i, (_, pred) in enumerate(GATES):
        if pred(reason):
            return i
    return len(GATES)   # unknown reason → treat as after all known gates


def _val(row, gate_name):
    """Human value string for a gate from the candidate row."""
    g = row
    if gate_name.startswith('Location'):
        return f"near_level={g['near_level']}  dist={g['dist_pct']}%"
    if gate_name.startswith('Contract Low'):
        return f"low_dist={g['contract_low_distance']}  (chased if >{config.CONTRACT_LOW_MAX_DIST})"
    if gate_name == 'Volume':
        return (f"peak1m={g['peak_1m']} vol3m={g['vol_3m']} vol5m={g['vol_5m']} "
                f"event_share={g['event_share']} persist={g['persistent_bg']} "
                f"path={g['gate_path']} notional=${g['premium_notional']}")
    if gate_name.startswith('Historical Value'):
        return f"hv_pctile={g['hv_pctile']}  (threshold {config.HIST_VALUE_PCTILE_MAX})"
    return ""   # short-cover / countertrend carry no stored telemetry


# Gates whose value string is real telemetry (worth showing even on PASS).
_TELEMETRY_GATES = {'Location (near level)', 'Contract Low / chased (§12)', 'Volume', 'Historical Value (§13)'}


def _card(row, lead):
    """Render the per-gate PASS/FAIL card for one minute in the requested format."""
    idx = _fail_index(row['blocked_reason'])
    lines = []
    for i, (name, _) in enumerate(GATES):
        if idx is None or i < idx:
            verdict = 'PASS'
        elif i == idx:
            verdict = 'FAIL'
        else:
            verdict = '–  (not evaluated — short-circuit)'
        v = _val(row, name)
        show = v and (verdict == 'FAIL' or name in _TELEMETRY_GATES)
        lines.append(f"  {name:26s} {verdict}" + (f"   {v}" if show else ""))
    # Leadership is context (not a level-path gate); show it if we have it.
    if lead:
        cl, pt = lead
        dom = 'CALL' if (cl or 0) >= (pt or 0) else 'PUT'
        lines.append(f"  {'Leadership (context)':26s} call={cl} put={pt} → {dom} leads")
    tail = ('FIRED ✅' if row['alert_fired'] else
            'PASSED level path → entered Gold gate' if row['blocked_reason'] == 'PASSED' else
            f"REJECTED at {GATES[idx][0]}" if idx is not None and idx < len(GATES) else
            f"REJECTED ({row['blocked_reason']})")
    lines.append(f"  {'Final':26s} {tail}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol'); ap.add_argument('strike', type=float)
    ap.add_argument('side'); ap.add_argument('date')
    ap.add_argument('--all-minutes', action='store_true', help='print every evaluated minute, not just the trace summary')
    a = ap.parse_args()
    side = {'C': 'CALL', 'P': 'PUT'}.get(a.side.upper(), a.side.upper())

    c = _conn(); cur = c.cursor()
    from psycopg2.extras import RealDictCursor
    dcur = c.cursor(cursor_factory=RealDictCursor)

    dcur.execute("""SELECT * FROM signal_candidates
                    WHERE symbol=%s AND candidate_side=%s AND strike=%s AND session_date=%s
                    ORDER BY ts""", (a.symbol, side, a.strike, a.date))
    rows = dcur.fetchall()

    # Leadership by minute (symbol-level).
    cur.execute("""SELECT date_trunc('minute', bar_time AT TIME ZONE 'America/Chicago'),
                          call_leadership, put_leadership
                   FROM volume_leadership WHERE symbol=%s AND session_date=%s""",
                (a.symbol, a.date))
    lead = {t: (cl, pt) for t, cl, pt in cur.fetchall()}

    print(f"\n{'='*72}\nFORENSICS: {a.symbol} {a.strike:g}{side[0]}  {a.date}\n{'='*72}")

    if not rows:
        # Off-level fallback: was it in the coverage log at all?
        cur.execute("""SELECT count(*), min(vol_1m), max(vol_1m), min(nearest_level_dist_pct)
                       FROM candidate_coverage WHERE symbol=%s AND option_type=%s AND strike=%s AND session_date=%s""",
                    (a.symbol, side, a.strike, a.date))
        n, vlo, vhi, nd = cur.fetchone()
        if n:
            print(f"\nNOT evaluated by the level path. Coverage log: {n} poll(s), "
                  f"1-min vol {vlo}-{vhi}, nearest morning level {nd}% away.")
            print("→ OFF-LEVEL: the primary path only evaluates level strikes; this strike "
                  "was watched but never entered the candidate pipeline.")
        else:
            print("\nNO RECORD in signal_candidates OR candidate_coverage.")
            print("→ Either it was outside the watched window (never fetched), or it predates "
                  "the coverage log (logging began 2026-07-19). Cannot reconstruct.")
        c.close(); return

    fired = [r for r in rows if r['alert_fired']]
    reasons = {}
    for r in rows:
        reasons[r['blocked_reason']] = reasons.get(r['blocked_reason'], 0) + 1

    print(f"\nEvaluated by the level path: {len(rows)} minute(s).  "
          + ("FIRED ✅" if fired else "never fired."))
    print("\nBlocked-reason breakdown (which gate stopped it, and how often):")
    for reason, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {n:3d} ×  {reason}")

    # Best would-be entry: valid volume + closest to its low.
    valids = [r for r in rows if r['valid_volume_event']]
    best = (min(valids, key=lambda r: r['contract_low_distance'] or 9e9) if valids
            else max(rows, key=lambda r: r['premium_notional'] or 0))
    # match leadership on the CST minute (volume_leadership is keyed by CST minute)
    lmin = best['ts'].astimezone(_cst()).replace(second=0, microsecond=0, tzinfo=None)
    print(f"\nCleanest candidate minute — {best['ts'].astimezone(_cst()):%H:%M} CST "
          f"(low_dist={best['contract_low_distance']}, notional=${best['premium_notional']}):")
    print(_card(best, lead.get(lmin)))

    # Gold-layer audit — for any signal on this contract that reached the Gold gate,
    # the base card ends at PASSED; signal_gate_audit carries the downstream verdict.
    cur.execute("""SELECT s.signal_time, sga.decision, sga.blocking_gate, sga.summary
                   FROM signals s JOIN signal_gate_audit sga ON sga.signal_id = s.id
                   WHERE s.symbol=%s AND (s.traded_strike=%s OR s.level_price=%s)
                     AND s.option_type=%s AND s.signal_time::date=%s
                   ORDER BY s.signal_time""", (a.symbol, a.strike, a.strike, side, a.date))
    ga = cur.fetchall()
    if ga:
        print("\nGold-layer gate audit (for signals that passed the base gates):")
        for st, dec, blk, summ in ga:
            tail = f" — blocked at {blk}" if blk else ""
            print(f"  {st.astimezone(_cst()):%H:%M} → {dec}{tail}\n      {summ}")

    if a.all_minutes:
        print(f"\n{'-'*72}\nPer-minute trace:")
        print(f"  {'time':5s} {'low_dist':8s} {'valid':5s} {'evshare':7s} {'hv':6s}  blocked_reason")
        for r in rows:
            print(f"  {r['ts'].astimezone(_cst()):%H:%M} {str(r['contract_low_distance'] or ''):>8s} "
                  f"{str(r['valid_volume_event'])[:5]:5s} {str(r['event_share'] or ''):>7s} "
                  f"{str(r['hv_pctile'] or ''):>6s}  {r['blocked_reason']}")
    print()
    c.close()


def _cst():
    import pytz
    return pytz.timezone('America/Chicago')


if __name__ == '__main__':
    main()

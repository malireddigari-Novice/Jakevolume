"""
Read-only verification of the first morning run on feature/oi-briefing-weekend-gaps.
Run AFTER the ~8:20 AM job:  python verify_monday_run.py [YYYY-MM-DD]
Writes nothing; only reads Postgres + re-renders the briefing levels from stored data.

Checks:
  1. near_oi_snapshots captured a baseline for all symbols (weekend-gap feature armed)
  2. nearest-expiry overnight OI buildup populated (reconcile weekend/holiday lookback fix)
  3. proximity-ordered levels + OI-wall star render correctly (R1/S1 = nearest, * = top OI)
  4. weekend gaps — expected EMPTY this first Monday (no prior Friday near-OI baseline yet)
"""
import sys
from datetime import date

import config
import db.ops as db
from data.market_utils import today_cst
from output.discord_notifier import _level_lines

db.init_pool()
day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else today_cst()
symbols = list(config.SYMBOLS)

print(f"\n=== Verifying morning run for {day} ({len(symbols)} symbols) ===\n")

conn = db._get()
with conn.cursor() as cur:
    # 1. near-OI baseline
    cur.execute("SELECT COUNT(DISTINCT symbol), COUNT(*), COUNT(DISTINCT expiry_date) "
                "FROM near_oi_snapshots WHERE snap_date = %s", (day,))
    nsym, nrows, nexp = cur.fetchone()
    c1 = nsym >= len(symbols)
    print(f"[{'PASS' if c1 else 'FAIL'}] 1. near_oi_snapshots baseline: "
          f"{nsym}/{len(symbols)} symbols, {nrows} rows, {nexp} expiries")

    # 2. reconcile populated nearest-expiry oi_change (was empty every Monday pre-fix)
    cur.execute("SELECT COUNT(*) FROM option_chain_snapshots "
                "WHERE snap_date = %s AND oi_change IS NOT NULL", (day,))
    recon = cur.fetchone()[0]
    cur.execute("SELECT MAX(snap_date) FROM option_chain_snapshots WHERE snap_date < %s", (day,))
    prior = cur.fetchone()[0]
    c2 = recon > 0
    print(f"[{'PASS' if c2 else 'FAIL'}] 2. overnight OI buildup reconciled: "
          f"{recon} rows with oi_change (prior session = {prior})")

    # 3. levels present + proximity render
    cur.execute("SELECT COUNT(*) FROM oi_levels WHERE level_date = %s", (day,))
    nlevels = cur.fetchone()[0]
    c3 = nlevels > 0
    print(f"[{'PASS' if c3 else 'FAIL'}] 3. oi_levels stored: {nlevels} rows "
          f"(expect ~{len(symbols) * 6})")
db._put(conn)

# Render proximity briefing for the first symbol with data (visual spot-check of #3)
for sym in symbols:
    rows = db.get_today_levels(sym, day)  # RealDictCursor rows (dict per level)
    if not rows:
        continue
    levels = [{'level_type': r['level_type'], 'rank': r['rank'], 'strike': float(r['strike']),
               'open_interest': int(r['open_interest']), 'option_type': r['option_type']}
              for r in rows]
    sup = [l for l in levels if l['level_type'] == 'SUPPORT']
    res = [l for l in levels if l['level_type'] == 'RESISTANCE']
    print(f"\n    {sym} briefing render (nearest-first, * = top OI):")
    for line in _level_lines(res, 'R').split("\n"):
        print(f"      {line}")
    for line in _level_lines(sup, 'S').split("\n"):
        print(f"      {line}")
    break

# 4. weekend gaps — expected empty this first Monday
print()
any_gap, prior_seen = False, None
for sym in symbols:
    wk = db.get_weekend_oi_gaps(sym, day, config.WEEKEND_GAP_MIN_CONTRACTS,
                                config.WEEKEND_GAP_MIN_PCT, config.WEEKEND_GAP_TOP_N)
    prior_seen = prior_seen or wk.get('prior_session')
    if wk['gaps'] and (wk.get('gap_days') or 0) >= 2:
        any_gap = True
        print(f"    {sym}: {len(wk['gaps'])} weekend gap(s) since {wk['prior_session']}")
if not any_gap:
    print(f"[INFO ] 4. Weekend OI Gaps: none shown "
          f"(prior near-OI session = {prior_seen}). "
          f"{'Expected on the FIRST run (no Friday baseline yet).' if prior_seen is None else ''}")

print("\n=== Done. Also scan jakevolume.log for any 'Morning snapshot failed' / WARNING lines. ===")

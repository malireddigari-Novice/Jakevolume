"""
Backtest the VolumeStickoutScore over stored option_level_bars (real 1-min option
volume), tied to real per-trade P&L. Answers: would requiring VolumeStickoutScore
>= 0.75 (with the right-tail guard) raise win-rate / P&L vs firing on every signal,
and how selective is it?

For each fired signal with a priceable traded contract:
  - rebuild the scorer inputs from the contract's bars up to the entry minute
  - compute VolumeStickoutScore (analysis.volume_stickout)
  - simulate P&L under the live exit rules (−50% stop / exit1-2 / EOD), as in
    backtest_gates.py
Cohorts: ALL | STICKOUT_VALID(>=.75) | DROPPED(<.75 or not right-tail) | STRONG(>=.85)
and the combination with the §16 VWAP gate.
"""
import psycopg2, config
from analysis.signal_detector import compute_exit_targets
from analysis.volume_stickout import compute_stickout

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                        user=config.DB_USER, password=config.DB_PASSWORD)
cur = conn.cursor()
cur.execute("""SELECT id, symbol, signal_time, signal_type, traded_strike, option_type,
                      level_price, trigger_price FROM signals
               WHERE traded_strike IS NOT NULL ORDER BY signal_time""")
sigs = cur.fetchall()

_u, _o, _l = {}, {}, {}
def und(sym, d):
    if (sym, d) not in _u:
        cur.execute("""SELECT bar_time, high, low, spot_price, volume FROM price_bars
                       WHERE symbol=%s AND bar_time::date=%s AND spot_price IS NOT NULL
                       ORDER BY bar_time""", (sym, d)); _u[(sym, d)] = cur.fetchall()
    return _u[(sym, d)]
def opt(sym, d, k, ot):
    key = (sym, d, k, ot)
    if key not in _o:
        cur.execute("""SELECT bar_time, close, low, volume FROM option_level_bars
                       WHERE symbol=%s AND level_date=%s AND strike=%s AND option_type=%s
                       ORDER BY bar_time""", (sym, d, k, ot)); _o[key] = cur.fetchall()
    return _o[key]
def levels(sym, d):
    if (sym, d) not in _l:
        cur.execute("""SELECT level_type, strike FROM oi_levels
                       WHERE symbol=%s AND level_date=%s""", (sym, d))
        _l[(sym, d)] = [{'level_type': lt, 'strike': float(s)} for lt, s in cur.fetchall()]
    return _l[(sym, d)]


def simulate(styp, opt_after, und_by_min, e1, e2):
    if len(opt_after) < 2: return None
    entry = float(opt_after[0][1])
    if entry <= 0: return None
    stop, held, proceeds, e1done = 0.5 * entry, 1.0, 0.0, False
    for (t, c, lo, v) in opt_after[1:]:
        c, lo = float(c), float(lo)
        if held > 0 and lo <= stop:
            proceeds += held * stop; held = 0.0; break
        u = und_by_min.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            def _hit(lvl): return lvl is not None and ((uh >= lvl) if styp == 'BULLISH' else (ul <= lvl))
            if not e1done and _hit(e1):
                proceeds += 0.5 * c; held -= 0.5; e1done = True; stop = entry
            if e1done and held > 0 and _hit(e2):
                proceeds += held * c; held = 0.0; break
    if held > 0: proceeds += held * float(opt_after[-1][1])
    return (proceeds - entry) / entry * 100.0


R = []
skipped = 0
for sid, sym, st, styp, tstrike, otype, lprice, espot in sigs:
    day = st.date()
    ob, ub = opt(sym, day, tstrike, otype), und(sym, day)
    if not ob or not ub: skipped += 1; continue

    # entry index = first option bar at/after signal time
    eidx = next((i for i, b in enumerate(ob) if b[0] >= st), None)
    if eidx is None or eidx + 1 >= len(ob): skipped += 1; continue

    vols = [int(b[3]) for b in ob]
    prior = vols[:eidx]
    cur_vol = vols[eidx]
    win5 = sum(vols[max(0, eidx - 4):eidx + 1])
    last5 = vols[max(0, eidx - 4):eidx + 1]
    prior5m = [sum(vols[i:i + 5]) for i in range(0, max(0, eidx - 4))]  # windows before current
    lows = [float(b[2]) for b in ob[:eidx + 1] if b[2] is not None]
    price = float(ob[eidx][1])
    intlow = min(lows) if lows else price
    cld = price / max(intlow, 0.01)

    sc = compute_stickout(cur_vol, prior, prior, win5, last5, prior5m, cld, sym)

    # VWAP at signal
    num = den = 0.0
    for bt, hi, lo, sp, v in ub:
        if bt <= st: num += float(sp) * float(v or 0); den += float(v or 0)
    vwap = (num / den) if den > 0 else None
    aligned = None if vwap is None else (
        (float(espot) >= vwap) if styp == 'BULLISH' else (float(espot) <= vwap))

    und_by_min = {bt.replace(second=0, microsecond=0): (float(hi), float(lo))
                  for bt, hi, lo, sp, v in ub if bt > st}
    e1, e2 = compute_exit_targets(styp, float(espot), levels(sym, day))
    pnl = simulate(styp, ob[eidx:], und_by_min, e1, e2)
    if pnl is None: skipped += 1; continue

    R.append(dict(sym=sym, day=day, styp=styp, pnl=pnl,
                  score=sc['score'], valid=sc['valid'], strong=sc['strong'],
                  rt=sc.get('right_tail_ok', False), avail=sc.get('components_available', 'floor'),
                  pctile=sc.get('session_pctile'), vdom=sc.get('visual_dom'),
                  aligned=aligned))
conn.close()


def rep(name, rows):
    if not rows: print(f"  {name:22} n=0"); return
    n = len(rows); w = sum(1 for r in rows if r['pnl'] > 0)
    avg = sum(r['pnl'] for r in rows) / n
    dollars = sum(r['pnl'] / 100 * 1000 for r in rows)
    print(f"  {name:22} n={n:4}  win%={w/n*100:5.1f}  avgP&L%={avg:+7.2f}  $@1k={dollars:+9,.0f}")

print(f"\nPriceable signals: {len(R)}  (skipped {skipped})")
# component availability (the history-reality check)
full = sum(1 for r in R if r['avail'] == 'full')
print(f"Percentile term available (>=20 prior bars): {full}/{len(R)}  "
      f"({full/len(R)*100:.0f}%) — rest used the reduced (no-percentile) score\n")

print(f"  {'COHORT':22} {'n':>5}  {'win%':>5}  {'avgP&L%':>8}  {'$@1k':>9}")
print("  " + "-"*62)
rep("ALL (fires today)", R)
rep("STICKOUT_VALID >=.75", [r for r in R if r['valid']])
rep("  of which STRONG>=.85", [r for r in R if r['strong']])
rep("DROPPED (<.75 / not RT)", [r for r in R if not r['valid']])
print()
rep("VWAP gate only", [r for r in R if r['aligned']])
rep("VWAP + STICKOUT", [r for r in R if r['aligned'] and r['valid']])
print()
print("  --- right-tail check: are KEPT trades actually high-volume? ---")
val = [r for r in R if r['valid']]; drop = [r for r in R if not r['valid']]
def _avg(rows, k):
    xs = [r[k] for r in rows if r[k] is not None]
    return (sum(xs)/len(xs)) if xs else float('nan')
print(f"  kept   (n={len(val)}): avg VisualDom={_avg(val,'vdom'):.2f}  avg SessionPctile={_avg(val,'pctile'):.0f}")
print(f"  dropped(n={len(drop)}): avg VisualDom={_avg(drop,'vdom'):.2f}  avg SessionPctile={_avg(drop,'pctile'):.0f}")

# selectivity by score bucket
print("\n  --- P&L by score bucket ---")
for lo, hi in [(0.0,0.5),(0.5,0.75),(0.75,0.85),(0.85,1.01)]:
    rep(f"score [{lo:.2f},{hi:.2f})", [r for r in R if lo <= r['score'] < hi])
print()

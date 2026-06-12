"""
Backtest the 3-rule ENTRY VOLUME GATE FIX (SignalDetector._eval_volume) over stored
option_level_bars + real per-trade P&L. Answers: of the signals that fired live, how
many pass the new 3-rule gate, and does requiring it improve win-rate / P&L?

Replays _eval_volume on the traded contract's real per-minute volume up to the entry
minute (option_level_bars 1-min volume IS per-minute, matching the detector's input).
Same exit simulation as backtest_volume_stickout.py (−50% stop / exit1-2 / EOD).

CAVEAT: scores already-fired signals (pre-filtered by old logic), small n, one
down-regime; a true gate test needs a full-candidate replay.
"""
import psycopg2, config
from analysis.signal_detector import compute_exit_targets, SignalDetector

D = SignalDetector()
EXC = config.STAIRSTEP_EXCITATION_MIN

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                        user=config.DB_USER, password=config.DB_PASSWORD)
cur = conn.cursor()
cur.execute("""SELECT id, symbol, signal_time, signal_type, traded_strike, option_type,
                      level_price, trigger_price FROM signals
               WHERE traded_strike IS NOT NULL ORDER BY signal_time""")
sigs = cur.fetchall()

_u, _o, _l = {}, {}, {}
def und(s, d):
    if (s, d) not in _u:
        cur.execute("""SELECT bar_time, high, low, spot_price, volume FROM price_bars
                       WHERE symbol=%s AND bar_time::date=%s AND spot_price IS NOT NULL
                       ORDER BY bar_time""", (s, d)); _u[(s, d)] = cur.fetchall()
    return _u[(s, d)]
def opt(s, d, k, ot):
    key = (s, d, k, ot)
    if key not in _o:
        cur.execute("""SELECT bar_time, close, low, volume FROM option_level_bars
                       WHERE symbol=%s AND level_date=%s AND strike=%s AND option_type=%s
                       ORDER BY bar_time""", (s, d, k, ot)); _o[key] = cur.fetchall()
    return _o[key]
def levels(s, d):
    if (s, d) not in _l:
        cur.execute("SELECT level_type, strike FROM oi_levels WHERE symbol=%s AND level_date=%s", (s, d))
        _l[(s, d)] = [{'level_type': lt, 'strike': float(x)} for lt, x in cur.fetchall()]
    return _l[(s, d)]

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
            def hit(lvl): return lvl is not None and ((uh >= lvl) if styp == 'BULLISH' else (ul <= lvl))
            if not e1done and hit(e1):
                proceeds += 0.5 * c; held -= 0.5; e1done = True; stop = entry
            if e1done and held > 0 and hit(e2):
                proceeds += held * c; held = 0.0; break
    if held > 0: proceeds += held * float(opt_after[-1][1])
    return (proceeds - entry) / entry * 100.0

R, skipped, reasons = [], 0, {}
for sid, sym, st, styp, tstrike, otype, lprice, espot in sigs:
    day = st.date()
    ob, ub = opt(sym, day, tstrike, otype), und(sym, day)
    if not ob or not ub: skipped += 1; continue
    eidx = next((i for i, b in enumerate(ob) if b[0] >= st), None)
    if eidx is None or eidx + 1 >= len(ob): skipped += 1; continue

    vols = [int(b[3]) for b in ob[:eidx + 1]]      # per-minute vols up to & incl entry
    delta = vols[-1]
    lows = [float(b[2]) for b in ob[:eidx + 1] if b[2] is not None]
    price = float(ob[eidx][1])
    cld = price / max(min(lows) if lows else price, 0.01)

    ev = D._eval_volume(sym, vols, delta, cld, 300, 300, 3.0, EXC)
    reasons[ev['block_reason']] = reasons.get(ev['block_reason'], 0) + 1

    und_by_min = {bt.replace(second=0, microsecond=0): (float(hi), float(lo))
                  for bt, hi, lo, sp, v in ub if bt > st}
    e1, e2 = compute_exit_targets(styp, float(espot), levels(sym, day))
    pnl = simulate(styp, ob[eidx:], und_by_min, e1, e2)
    if pnl is None: skipped += 1; continue
    R.append(dict(sym=sym, styp=styp, pnl=pnl, valid=ev['valid'],
                  block=ev['block_reason'], trig=ev['trigger_type']))
conn.close()

def rep(name, rows):
    if not rows: print(f"  {name:24} n=0"); return
    n = len(rows); w = sum(1 for r in rows if r['pnl'] > 0)
    avg = sum(r['pnl'] for r in rows) / n
    dollars = sum(r['pnl'] / 100 * 1000 for r in rows)
    print(f"  {name:24} n={n:4}  win%={w/n*100:5.1f}  avgP&L%={avg:+7.2f}  $@1k={dollars:+9,.0f}")

print(f"\nPriceable signals: {len(R)}  (skipped {skipped})")
print(f"  {'COHORT':24} {'n':>5}  {'win%':>5}  {'avgP&L%':>8}  {'$@1k':>9}")
print("  " + "-"*64)
rep("ALL (fires today)", R)
rep("3-RULE VALID (fires now)", [r for r in R if r['valid']])
rep("3-RULE BLOCKED", [r for r in R if not r['valid']])
print()
rep("  trigger SINGLE_BAR", [r for r in R if r['valid'] and r['trig'] == 'SINGLE_BAR'])
rep("  trigger 5-BAR WINDOW", [r for r in R if r['valid'] and r['trig'] == 'FIVE_BAR_WINDOW'])
print("\n  block-reason distribution (all signals):")
for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
    print(f"    {k:24} {v}")
print(f"\n  Selectivity: 3-rule would fire {sum(1 for r in R if r['valid'])}/{len(R)} "
      f"({sum(1 for r in R if r['valid'])/len(R)*100:.0f}%) of historical signals")
print()

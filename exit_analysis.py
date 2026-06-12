"""
Exit-criteria analysis: how much of each trade's move does the CURRENT exit capture,
and would alternative exits make more? Uses the backfilled option price paths
(option_level_bars) for the traded contracts of the 97 priceable signals.

Current rule modeled: -50% option stop, sell half at the shifted 1st opposing level,
remainder at the 2nd (or opposite-side volume), EOD close; stop→breakeven after exit1.
Alternatives are full-position option-price rules, compared on $ per $1k premium/trade.
"""
import psycopg2, config
from analysis.signal_detector import compute_exit_targets

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                        user=config.DB_USER, password=config.DB_PASSWORD)
cur = conn.cursor()
cur.execute("""SELECT id, symbol, signal_time, signal_type, traded_strike, option_type,
                      trigger_price FROM signals WHERE traded_strike IS NOT NULL ORDER BY signal_time""")
sigs = cur.fetchall()
_u, _o, _l = {}, {}, {}
def und(s, d):
    if (s, d) not in _u:
        cur.execute("""SELECT bar_time, high, low FROM price_bars WHERE symbol=%s AND bar_time::date=%s
                       AND spot_price IS NOT NULL ORDER BY bar_time""", (s, d)); _u[(s, d)] = cur.fetchall()
    return _u[(s, d)]
def opt(s, d, k, ot):
    key = (s, d, k, ot)
    if key not in _o:
        cur.execute("""SELECT bar_time, high, low, close FROM option_level_bars
                       WHERE symbol=%s AND level_date=%s AND strike=%s AND option_type=%s
                       ORDER BY bar_time""", (s, d, k, ot)); _o[key] = cur.fetchall()
    return _o[key]
def levels(s, d):
    if (s, d) not in _l:
        cur.execute("SELECT level_type, strike FROM oi_levels WHERE symbol=%s AND level_date=%s", (s, d))
        _l[(s, d)] = [{'level_type': lt, 'strike': float(x)} for lt, x in cur.fetchall()]
    return _l[(s, d)]

# ── Exit simulators (return % P&L on premium) ──────────────────────────────────
def cur_rule(styp, entry, opath, ubymin, e1, e2):
    """Current: half at e1 (underlying), half at e2, -50% stop, stop->BE after e1, EOD."""
    stop, held, proc, e1done = 0.5*entry, 1.0, 0.0, False
    for (t, h, l, c) in opath[1:]:
        if held > 0 and l <= stop: proc += held*stop; held = 0; break
        u = ubymin.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            def hit(x): return x is not None and ((uh >= x) if styp=='BULLISH' else (ul <= x))
            if not e1done and hit(e1): proc += 0.5*c; held -= 0.5; e1done = True; stop = entry
            if e1done and held > 0 and hit(e2): proc += held*c; held = 0; break
    if held > 0: proc += held*opath[-1][3]
    return (proc-entry)/entry*100

def cur_rule_premium(styp, entry, opath, ubymin, e1, e2, tp=0.50):
    """Current rule + premium-spike exit1: exit1 also fires when the option mark gains
    >= tp (sell half at that price, stop->BE), even if the underlying never reaches e1."""
    stop, held, proc, e1done = 0.5*entry, 1.0, 0.0, False
    for (t, h, l, c) in opath[1:]:
        if held > 0 and l <= stop: proc += held*stop; held = 0; break
        if not e1done and h >= entry*(1+tp):           # premium spike -> bank half at +tp
            proc += 0.5*entry*(1+tp); held -= 0.5; e1done = True; stop = entry
        u = ubymin.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            def hit(x): return x is not None and ((uh >= x) if styp == 'BULLISH' else (ul <= x))
            if not e1done and hit(e1): proc += 0.5*c; held -= 0.5; e1done = True; stop = entry
            if e1done and held > 0 and hit(e2): proc += held*c; held = 0; break
    if held > 0: proc += held*opath[-1][3]
    return (proc-entry)/entry*100

def opt_rule(entry, opath, stop_pct, tp_pct=None, trail_arm=None, trail_pct=None, be_after=None):
    """Full-position option-price rule."""
    stop = entry*(1-stop_pct); peak = entry
    for (t, h, l, c) in opath[1:]:
        if l <= stop: return (stop/entry-1)*100
        if tp_pct and h >= entry*(1+tp_pct): return tp_pct*100
        peak = max(peak, h)
        if be_after and peak >= entry*(1+be_after) and stop < entry: stop = entry
        if trail_arm and trail_pct and peak >= entry*(1+trail_arm):
            ts = peak*(1-trail_pct)
            if l <= ts: return (ts/entry-1)*100
    return (opath[-1][3]/entry-1)*100

def ladder(entry, opath, legs, stop_pct=0.50, trail_arm=None, trail_pct=None):
    """Scale out at option-price take-profit legs [(tp_pct, qty_frac), ...]; remainder
    runs to a trail (if set) or EOD. -50% hard stop on whatever is still held."""
    stop, held, proc, peak = entry*(1-stop_pct), 1.0, 0.0, entry
    legs = sorted(legs); li = 0
    for (t, h, l, c) in opath[1:]:
        if held > 0 and l <= stop: proc += held*stop; held = 0; break
        while li < len(legs) and held > 0 and h >= entry*(1+legs[li][0]):
            q = min(legs[li][1], held); proc += q*entry*(1+legs[li][0]); held -= q; li += 1
        peak = max(peak, h)
        if trail_arm and trail_pct and held > 0 and peak >= entry*(1+trail_arm):
            ts = peak*(1-trail_pct)
            if l <= ts: proc += held*ts; held = 0; break
    if held > 0: proc += held*opath[-1][3]
    return (proc-entry)/entry*100

rows = []
for sid, sym, st, styp, k, ot, espot in sigs:
    ob, ub = opt(sym, st.date(), k, ot), und(sym, st.date())
    if not ob or not ub: continue
    ei = next((i for i, b in enumerate(ob) if b[0] >= st), None)
    if ei is None or ei+1 >= len(ob): continue
    opath = ob[ei:]
    entry = float(opath[0][3])
    if entry <= 0: continue
    ubymin = {bt.replace(second=0, microsecond=0): (float(h), float(l)) for bt, h, l in ub if bt > st}
    e1, e2 = compute_exit_targets(styp, float(espot), levels(sym, st.date()))
    # Nearest two opposing levels (NOT skipping the closest) for the NEAR_EXITS test
    lv = levels(sym, st.date()); sp = float(espot)
    if styp == 'BULLISH':
        opp = sorted(x['strike'] for x in lv if x['level_type'] == 'RESISTANCE' and x['strike'] > sp)
    else:
        opp = sorted((x['strike'] for x in lv if x['level_type'] == 'SUPPORT' and x['strike'] < sp), reverse=True)
    e1n = opp[0] if opp else e1
    e2n = opp[1] if len(opp) > 1 else e2
    mfe = (max(float(b[1]) for b in opath[1:])/entry-1)*100 if len(opath) > 1 else 0.0
    mae = (min(float(b[2]) for b in opath[1:])/entry-1)*100 if len(opath) > 1 else 0.0
    op = [(t, float(h), float(l), float(c)) for t, h, l, c in opath]
    rows.append({
        'mfe': mfe, 'mae': mae,
        'CURRENT':   cur_rule(styp, entry, op, ubymin, e1, e2),
        'CUR+PREM50': cur_rule_premium(styp, entry, op, ubymin, e1, e2, tp=0.50),
        'CUR+PREM75': cur_rule_premium(styp, entry, op, ubymin, e1, e2, tp=0.75),
        'NEAR_EXITS': cur_rule(styp, entry, op, ubymin, e1n, e2n),
        'STOP35':    opt_rule(entry, op, 0.35),
        'TP100_S50': opt_rule(entry, op, 0.50, tp_pct=1.00),
        'TRAIL':     opt_rule(entry, op, 0.50, trail_arm=0.40, trail_pct=0.30, be_after=0.30),
        'LADDER_50_100': ladder(entry, op, [(0.50, 0.5), (1.00, 0.5)]),
        'LADDER_RUN':    ladder(entry, op, [(0.50, 0.5)], trail_arm=0.60, trail_pct=0.40),
        'LADDER_3':      ladder(entry, op, [(0.40, 0.34), (0.90, 0.33)], trail_arm=0.90, trail_pct=0.40),
        'EOD_S50':       ladder(entry, op, []),                       # -50% stop, else hold to EOD
        'CHIP_RUN':      ladder(entry, op, [(1.00, 0.25)]),           # bank 1/4 at +100%, run 3/4 to EOD
        'CEILING':   mfe,    # theoretical: sell at the peak
    })
conn.close()

n = len(rows)
print(f"\nExit analysis over {n} priceable signals\n")
print(f"  MFE (avg peak gain available): {sum(r['mfe'] for r in rows)/n:+.1f}%   "
      f"median {sorted(r['mfe'] for r in rows)[n//2]:+.1f}%")
print(f"  MAE (avg worst drawdown):      {sum(r['mae'] for r in rows)/n:+.1f}%   "
      f"median {sorted(r['mae'] for r in rows)[n//2]:+.1f}%")
print(f"\n  {'RULE':12} {'avgP&L%':>8} {'medP&L%':>8} {'win%':>6} {'$@1k total':>11}")
print("  " + "-"*52)
for rule in ['CURRENT', 'CUR+PREM50', 'CUR+PREM75', 'NEAR_EXITS', 'STOP35', 'TP100_S50', 'TRAIL',
             'LADDER_50_100', 'LADDER_RUN', 'LADDER_3', 'EOD_S50', 'CHIP_RUN', 'CEILING']:
    vals = [r[rule] for r in rows]
    avg = sum(vals)/n
    med = sorted(vals)[n//2]
    win = sum(1 for v in vals if v > 0)/n*100
    dollars = sum(v/100*1000 for v in vals)
    print(f"  {rule:12} {avg:+8.2f} {med:+8.2f} {win:6.1f} {dollars:+11,.0f}")

# how much of MFE does the current rule capture?
cap = [r['CURRENT']/r['mfe'] for r in rows if r['mfe'] > 5]
print(f"\n  Of trades with MFE>5%, current rule captured avg "
      f"{sum(cap)/len(cap)*100:.0f}% of the peak (n={len(cap)})")
winners = [r for r in rows if r['mfe'] > 30]
print(f"  Trades whose peak exceeded +30%: {len(winners)}/{n} "
      f"(avg peak {sum(r['mfe'] for r in winners)/len(winners):+.0f}%, "
      f"current captured {sum(r['CURRENT'] for r in winners)/len(winners):+.0f}%)")
print()

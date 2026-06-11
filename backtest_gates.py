"""
Backtest: would a VWAP trend-gate have improved REAL option P&L?

Now that option_level_bars is backfilled from Alpaca (traded contracts included),
this simulates each signal's actual dollar outcome under the LIVE exit rules, on
the traded contract's real 1-min price path:

  entry      : traded contract (signals.traded_strike, option_type), close of the
               bar at/after signal_time  (matches observed fills within ~1c)
  stoploss   : option mark <= 0.5 * entry  (main.py:550) → close remaining at stop
  exit1      : underlying reaches the shifted 1st opposing level (compute_exit_targets)
               → sell half, move stop to breakeven (entry)
  exit2      : underlying reaches the 2nd opposing level → sell remainder
  EOD        : 0DTE → close any remainder at the last bar
  ordering   : within a bar, stop is checked before targets (conservative)

P&L is per-trade % of premium (one contract, spread ignored — close-to-close, so
mildly optimistic by ~half-spread/leg). Cohorts compare the VWAP gate:
  ALL / VWAP_ALIGNED (gate keeps) / VWAP_AGAINST (gate cuts) / BULLISH / BEARISH.
"""
import psycopg2, config
from analysis.signal_detector import compute_exit_targets

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                        user=config.DB_USER, password=config.DB_PASSWORD)
cur = conn.cursor()

cur.execute("""
    SELECT id, symbol, signal_time, signal_type, traded_strike, option_type,
           level_price, trigger_price, pc_conviction
    FROM signals
    WHERE traded_strike IS NOT NULL
    ORDER BY signal_time
""")
sigs = cur.fetchall()

_und_cache, _opt_cache, _lvl_cache = {}, {}, {}

def und_bars(sym, d):
    k = (sym, d)
    if k not in _und_cache:
        cur.execute("""SELECT bar_time, high, low, close, spot_price, volume
                       FROM price_bars WHERE symbol=%s AND bar_time::date=%s
                       AND spot_price IS NOT NULL ORDER BY bar_time""", (sym, d))
        _und_cache[k] = cur.fetchall()
    return _und_cache[k]

def opt_bars(sym, d, strike, otype):
    k = (sym, d, strike, otype)
    if k not in _opt_cache:
        cur.execute("""SELECT bar_time, open, high, low, close, volume
                       FROM option_level_bars
                       WHERE symbol=%s AND level_date=%s AND strike=%s AND option_type=%s
                       ORDER BY bar_time""", (sym, d, strike, otype))
        _opt_cache[k] = cur.fetchall()
    return _opt_cache[k]

def day_levels(sym, d):
    k = (sym, d)
    if k not in _lvl_cache:
        cur.execute("""SELECT level_type, strike FROM oi_levels
                       WHERE symbol=%s AND level_date=%s""", (sym, d))
        _lvl_cache[k] = [{'level_type': lt, 'strike': float(s)} for lt, s in cur.fetchall()]
    return _lvl_cache[k]


def _hit(signal_type, level, u_high, u_low):
    """Did the underlying reach a target level this bar?"""
    return (u_high >= level) if signal_type == 'BULLISH' else (u_low <= level)


def simulate(signal_type, opt_path, und_by_min, exit1_u, exit2_u):
    """Return per-trade % return on premium under the live exit rules, or None."""
    if len(opt_path) < 2:
        return None
    entry = float(opt_path[0][4])          # close of entry bar
    if entry <= 0:
        return None
    stop = 0.5 * entry
    held = 1.0
    proceeds = 0.0
    exit1_done = False
    for (t, o, h, l, c, v) in opt_path[1:]:           # bars after entry
        l, c = float(l), float(c)
        if held > 0 and l <= stop:                    # stop (option intrabar low)
            proceeds += held * stop
            held = 0.0
            break
        u = und_by_min.get(t.replace(second=0, microsecond=0))
        if u:
            uh, ul = u
            if not exit1_done and exit1_u is not None and _hit(signal_type, exit1_u, uh, ul):
                proceeds += 0.5 * c                    # sell half at option close
                held -= 0.5
                exit1_done = True
                stop = entry                           # move stop to breakeven
            if exit1_done and exit2_u is not None and held > 0 and _hit(signal_type, exit2_u, uh, ul):
                proceeds += held * c
                held = 0.0
                break
    if held > 0:                                       # EOD close
        proceeds += held * float(opt_path[-1][4])
    return (proceeds - entry) / entry * 100.0


results = []
skipped = 0
for sid, sym, st, styp, tstrike, otype, lprice, entry_spot, conv in sigs:
    day = st.date()
    ub = und_bars(sym, day)
    ob = opt_bars(sym, day, tstrike, otype)
    if not ub or not ob:
        skipped += 1
        continue

    # VWAP up to & including the signal bar
    num = den = 0.0
    for bt, hi, lo, cl, sp, vol in ub:
        if bt <= st:
            num += float(sp) * float(vol or 0); den += float(vol or 0)
    if den <= 0:
        skipped += 1
        continue
    vwap = num / den

    # Underlying minute map AFTER the signal (for exit-target detection)
    und_by_min = {bt.replace(second=0, microsecond=0): (float(hi), float(lo))
                  for bt, hi, lo, cl, sp, vol in ub if bt > st}

    # Option path = entry bar (first >= signal_time) + forward bars
    opt_path = [r for r in ob if r[0] >= st]
    if len(opt_path) < 2:
        skipped += 1
        continue

    exit1_u, exit2_u = compute_exit_targets(styp, float(entry_spot), day_levels(sym, day))
    pnl = simulate(styp, opt_path, und_by_min, exit1_u, exit2_u)
    if pnl is None:
        skipped += 1
        continue

    vwap_aligned = (float(entry_spot) >= vwap) if styp == 'BULLISH' else (float(entry_spot) <= vwap)
    results.append(dict(sid=sid, sym=sym, day=day, styp=styp, conv=conv,
                        pnl=pnl, vwap_aligned=vwap_aligned))

conn.close()


def report(name, rows):
    if not rows:
        print(f"  {name:18} n=0"); return
    n = len(rows)
    wins = sum(1 for r in rows if r['pnl'] > 0)
    avg = sum(r['pnl'] for r in rows) / n
    med = sorted(r['pnl'] for r in rows)[n // 2]
    # cumulative $ if you risked a fixed $1,000 premium per trade
    dollars = sum(r['pnl'] / 100 * 1000 for r in rows)
    print(f"  {name:18} n={n:4}  win%={wins/n*100:5.1f}  avgP&L%={avg:+7.2f}  "
          f"medP&L%={med:+7.2f}  $@1k/trade={dollars:+10,.0f}")

R = results
print(f"\nPriceable signals: {len(R)}  (skipped {skipped} lacking bars)")
print("Exit model: -50% option stop | half-off at exit1/exit2 underlying levels | EOD close\n")
print(f"  {'COHORT':18} {'n':>5}  {'win%':>5}  {'avgP&L%':>8}  {'medP&L%':>8}  {'$@1k/trade':>11}")
print("  " + "-"*74)
report("ALL (baseline)", R)
report("VWAP_ALIGNED",   [r for r in R if r['vwap_aligned']])
report("VWAP_AGAINST",   [r for r in R if not r['vwap_aligned']])
print()
report("BULLISH all",    [r for r in R if r['styp'] == 'BULLISH'])
report("  BULL aligned",  [r for r in R if r['styp'] == 'BULLISH' and r['vwap_aligned']])
report("  BULL against",  [r for r in R if r['styp'] == 'BULLISH' and not r['vwap_aligned']])
report("BEARISH all",    [r for r in R if r['styp'] == 'BEARISH'])
report("  BEAR aligned",  [r for r in R if r['styp'] == 'BEARISH' and r['vwap_aligned']])
report("  BEAR against",  [r for r in R if r['styp'] == 'BEARISH' and not r['vwap_aligned']])

# Validation: 2026-06-10 traded signals should ~reproduce the live -50% stop-outs
print("\n  --- 2026-06-10 (validation vs actual -$2,589 / ~-52% avg) ---")
Y = [r for r in R if str(r['day']) == '2026-06-10']
report("06-10 ALL", Y)
for r in sorted(Y, key=lambda x: x['sym']):
    keep = "KEEP" if r['vwap_aligned'] else "SKIP"
    print(f"    {r['sym']:5} {r['styp']:7} P&L%={r['pnl']:+7.2f}  vwap_aligned={str(r['vwap_aligned']):5} -> {keep}")
print()

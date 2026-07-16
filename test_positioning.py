"""Fresh-OI positioning engine test. Run: python test_positioning.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis import positioning as p

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

INV = dict(build_min=250, unwind_min=250, rotation_vol_min=1000, flat_oi_max=100)

# ── 1. inventory states ──
ck("BUILD", p.inventory_state(7000, 5000, **INV) == 'BUILD')
ck("UNWIND", p.inventory_state(-9000, 5000, **INV) == 'UNWIND')
ck("ROTATION (flat OI, heavy vol)", p.inventory_state(20, 12000, **INV) == 'ROTATION')
ck("STABLE (quiet)", p.inventory_state(10, 50, **INV) == 'STABLE')

def contract(strike, ot, oi, vol, mark):
    return {'strike': strike, 'option_type': ot, 'open_interest': oi, 'volume': vol, 'mark': mark}

# ── 2. AAPL: tight fresh CALL cluster 320-325 near spot 315 -> strong bullish ──
aapl = [contract(320, 'CALL', 20000, 9000, 3.0),
        contract(322.5, 'CALL', 18000, 8000, 2.5),
        contract(325, 'CALL', 16000, 7000, 2.0),
        contract(310, 'PUT', 12000, 500, 2.0)]     # small, no fresh OI
aapl_oi = {(320.0, 'CALL'): {'oi_change': 7234}, (322.5, 'CALL'): {'oi_change': 6948},
           (325.0, 'CALL'): {'oi_change': 6132}, (310.0, 'PUT'): {'oi_change': 40}}
h = p.heatmap('AAPL', aapl, aapl_oi, 315.0)
ck(f"AAPL dominant CALL", h['dominant_side'] == 'CALL')
ck(f"AAPL bull_score high ({h['bull_score']})", h['bull_score'] >= 8.0)
ck(f"AAPL concentration high ({h['concentration']})", h['concentration'] in ('HIGH', 'VERY_HIGH'))
ck("AAPL cluster 320-325", h['cluster_low'] == 320.0 and h['cluster_high'] == 325.0)
ck("AAPL 3 fresh strikes", h['fresh_count'] == 3)
print(f"     -> bull={h['bull_score']} bear={h['bear_score']} conc={h['concentration']} "
      f"cluster={h['cluster_low']}-{h['cluster_high']} net=${h['net_notional']:,} wdist={h['weighted_distance_pct']}")

# ── 3. GOOGL: fresh calls near spot + a FAR 170P hedge that must NOT flip it bearish ──
googl = [contract(360, 'CALL', 8000, 5000, 1.8), contract(365, 'CALL', 6000, 4000, 0.9),
         contract(370, 'CALL', 5000, 3000, 0.5), contract(170, 'PUT', 9000, 200, 0.5)]
googl_oi = {(360.0, 'CALL'): {'oi_change': 5000}, (365.0, 'CALL'): {'oi_change': 4000},
            (370.0, 'CALL'): {'oi_change': 3000}, (170.0, 'PUT'): {'oi_change': 7000}}
g = p.heatmap('GOOGL', googl, googl_oi, 358.0)
ck("GOOGL far 170P hedge does NOT flip bearish (dominant CALL)", g['dominant_side'] == 'CALL')
ck("GOOGL put weighted-notional ~0 (far hedge discounted)", g['put_notional'] < g['call_notional'] * 0.05)
print(f"     -> GOOGL dominant={g['dominant_side']} call_wn=${g['call_notional']:,} put_wn=${g['put_notional']:,}")

# ── 4. UNWIND surfaced ──
u = [contract(300, 'CALL', 5000, 3000, 1.0)]
u_oi = {(300.0, 'CALL'): {'oi_change': -8000}}
hu = p.heatmap('X', u, u_oi, 300.0)
ck("unwind captured, no bullish build", hu['fresh_count'] == 0 and len(hu['unwind']) == 1)

# ── 5. Layer-3 confidence hook ──
# AAPL heat-map h (bull 9.4, dominant CALL, cluster 320-325).
a_in  = p.confidence_adjustment('BULLISH', 322.5, h, align_bonus=15, contra_penalty=10)
ck("aligned bullish in-cluster -> ALIGNED + bonus", a_in['alignment'] == 'ALIGNED' and a_in['delta'] > 0)
a_off = p.confidence_adjustment('BULLISH', 300.0, h, align_bonus=15, contra_penalty=10)
ck("aligned but off-cluster -> smaller bonus", a_off['alignment'] == 'ALIGNED' and 0 < a_off['delta'] < a_in['delta'])
a_con = p.confidence_adjustment('BEARISH', 310.0, h, align_bonus=15, contra_penalty=10)
ck("bearish vs bullish positioning -> CONTRA + penalty", a_con['alignment'] == 'CONTRA' and a_con['delta'] == -10)
a_none = p.confidence_adjustment('BULLISH', 320.0, {'fresh_count': 0}, align_bonus=15, contra_penalty=10)
ck("no positioning -> NONE, delta 0", a_none['alignment'] == 'NONE' and a_none['delta'] == 0)
print(f"     -> in-cluster {a_in['delta']:+d}, off-cluster {a_off['delta']:+d}, contra {a_con['delta']:+d}")

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

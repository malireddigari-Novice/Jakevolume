"""Chain-leadership engine test. Run: python test_chain_leadership.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis import chain_leadership as cl

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def c(strike, vol, mark, low=1.0):
    return {'strike': strike, 'vol': vol, 'mark': mark,
            'notional': vol * mark * 100, 'low_dist': low}

# ── 1. GOOGL-style coordinated CALL leadership across 5 strikes (spot 358) ──
# No single strike dominates a 1000 floor, but together it's unmistakable call control.
calls = [c(355, 300, 4.5), c(357.5, 500, 3.0), c(360, 700, 1.8), c(362.5, 600, 1.0), c(365, 300, 0.6)]
puts  = [c(355, 40, 1.0), c(352.5, 30, 1.8)]   # quiet put side
v = cl.detect(calls, puts, spot=358.0, strike_min_vol=200, min_breadth=3,
              min_combined_vol=1500, min_notional=100_000, leadership_margin=1.5)
ck("CALL side controls the chain", v['controlling_side'] == 'CALL')
ck("breadth = 5 participating strikes", v['breadth'] == 5)
ck("combined volume = 2400", v['combined_volume'] == 2400)
ck("leader = max-notional strike (357.5C, $150k)", v['leader_strike'] == 357.5)
ck("recommended = one step beyond leader (360C convexity)", v['recommended_strike'] == 360.0)
ck("supporting strikes listed", v['supporting_strikes'] == [355, 357.5, 360, 362.5, 365])
ck("confidence high", v['confidence'] >= 60)
print(f"     -> {v['controlling_side']} lead={v['leader_strike']} rec={v['recommended_strike']} conf={v['confidence']}")

# ── 2. Single lone strike printing big = IGNORED (no breadth) ──
lone = [c(362.5, 1419, 0.81), c(360, 20, 1.8), c(357.5, 15, 3.0)]
v2 = cl.detect(lone, puts, spot=358.0, strike_min_vol=200, min_breadth=3,
               min_combined_vol=1500, min_notional=100_000, leadership_margin=1.5)
ck("lone big strike -> no leadership", v2['controlling_side'] is None and v2['reason'] == 'NO_COORDINATED_SIDE')

# ── 3. Both sides coordinated, neither dominates -> no call ──
both_calls = [c(360, 600, 2.0), c(362.5, 600, 1.5), c(365, 600, 1.0)]
both_puts  = [c(355, 600, 2.0), c(352.5, 600, 1.5), c(350, 600, 1.0)]
v3 = cl.detect(both_calls, both_puts, spot=357.5, strike_min_vol=200, min_breadth=3,
               min_combined_vol=1500, min_notional=100_000, leadership_margin=1.5)
ck("balanced both sides -> NO_DOMINANT_SIDE", v3['controlling_side'] is None and v3['reason'] == 'NO_DOMINANT_SIDE')

# ── 4. PUT leadership mirror ──
lead_puts = [c(355, 400, 2.0), c(352.5, 700, 1.4), c(350, 800, 0.9), c(347.5, 350, 0.5)]
quiet_calls = [c(360, 30, 1.5), c(362.5, 20, 1.0)]
v4 = cl.detect(quiet_calls, lead_puts, spot=356.0, strike_min_vol=200, min_breadth=3,
               min_combined_vol=1500, min_notional=100_000, leadership_margin=1.5)
ck("PUT side controls", v4['controlling_side'] == 'PUT')
ck("PUT recommended is one step beyond leader (below it)", v4['recommended_strike'] == 350.0)

# ── 5. Below thresholds -> nothing ──
weak = [c(360, 250, 1.0), c(362.5, 250, 1.0)]   # breadth 2 < 3
v5 = cl.detect(weak, [], spot=358.0, strike_min_vol=200, min_breadth=3,
               min_combined_vol=1500, min_notional=100_000, leadership_margin=1.5)
ck("insufficient breadth -> no leadership", v5['controlling_side'] is None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

"""
#2 VOLUME_LEADER + #5 economic-flow tests. Run: python test_volume_leader.py
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import economic_flow as ef, volume_leader as vl

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

# ── #5 economic flow: dollars, not contract count ──
w = ef.weighted_leadership([
    {'strike': 630, 'side': 'PUT',  'event_vol': 5000, 'mark': 0.05, 'event_share': 0.8},
    {'strike': 630, 'side': 'CALL', 'event_vol': 1000, 'mark': 1.00, 'event_share': 0.8},
], spot=630)
ck("$1.00 calls outweigh 5000 penny puts", w['dominant'] == 'CALL' and w['call_weight'] > w['put_weight'])

far = ef.weighted_leadership([{'strike': 650, 'side': 'PUT', 'event_vol': 4000, 'mark': 0.03,
                               'event_share': 0.5}], spot=630)
ck("far-OTM (>3%) contributes ~nothing", far['put_weight'] == 0)

ck("PDS recycled discounts weight",
   ef.directional_weight(event_vol=1000, mark=1.0, spot=630, strike=630, event_share=0.8,
                         pds_class='REPRICED_RECYCLED')
   < ef.directional_weight(event_vol=1000, mark=1.0, spot=630, strike=630, event_share=0.8,
                           pds_class='FRESH_ACCUMULATION'))

# ── #2 VOLUME_LEADER qualification ──
config.VOLUME_LEADER_1M_MIN['default'] = 1500
config.VOLUME_LEADER_NOTIONAL_MIN['default'] = 200000

def q(**kw):
    base = dict(moneyness_strikes=0, completed_vol=2000, premium_notional=400000, low_dist=1.1,
                event_share=0.7, persistent_bg=False, pds_class=None,
                same_weight=400000, opp_weight=50000)
    base.update(kw)
    return vl.qualifies(base.pop('symbol', 'GOOGL'), base.pop('side', 'PUT'), **base)

ck("GOOGL 370P positive control qualifies", q(pds_class='VIRGIN_DISCOVERY')['qualifies'])
ck("recycled premium blocked at fresh", q(pds_class='REPRICED_RECYCLED')['block'] == 'fresh')
ck("opposite outweighs -> blocked at leads", q(opp_weight=500000)['block'] == 'leads')
ck("chased -> blocked at premium_low", q(low_dist=2.5)['block'] == 'premium_low')
ck("sub-threshold vol+notional -> blocked at exceptional",
   q(completed_vol=500, premium_notional=50000)['block'] == 'exceptional')
ck("2-strikes-from-ATM -> blocked at moneyness", q(moneyness_strikes=2)['block'] == 'moneyness')

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

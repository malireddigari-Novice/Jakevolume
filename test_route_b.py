"""P3 Route B test — exceptional single-strike qualifier. Run: python test_route_b.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.route_b import route_b_qualifies

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

base = dict(peak_1m=2460, strikes_from_atm=1, premium_notional=100000,
            clow_region='STRONG_VALUE_LOCATION', concentrated=True, opposite_dominates=False)

ck("TSLA 425C-like qualifies", route_b_qualifies(**base) is True)
ck("MSFT 380C-like (0 strikes) qualifies", route_b_qualifies(**{**base, 'strikes_from_atm': 0}) is True)
ck("below 2000 peak -> no", route_b_qualifies(**{**base, 'peak_1m': 1500}) is False)
ck(">2 strikes from ATM -> no", route_b_qualifies(**{**base, 'strikes_from_atm': 3}) is False)
ck("chased value -> no", route_b_qualifies(**{**base, 'clow_region': 'LIKELY_CHASED_OR_LATE'}) is False)
ck("not concentrated -> no", route_b_qualifies(**{**base, 'concentrated': False}) is False)
ck("opposite dominates -> no", route_b_qualifies(**{**base, 'opposite_dominates': True}) is False)
ck("unknown clow region allowed (None)", route_b_qualifies(**{**base, 'clow_region': None}) is True)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

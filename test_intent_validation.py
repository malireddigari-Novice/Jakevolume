"""
P2 tests — directional-intent validation + opposite-side veto (§5-§9) and their
integration into the Gold gate. Run:  python test_intent_validation.py  (exit 0 = pass)

Includes control TEST A (TSLA put false-direction -> PROBABLE_PUT_SUPPLY, no PUT
alert) and TEST E (option-supply near high -> not directional -> research).
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import intent_validation as iv
from analysis import gold_mode as gm

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1


def obs(mark, iv_, spot, call_ld, put_ld, **extra):
    d = {'mark': mark, 'iv': iv_, 'spot': spot,
         'call_leadership': call_ld, 'put_leadership': put_ld}
    d.update(extra)
    return d


# ── classify_intent — demand positives ──
call_demand = iv.classify_intent('CALL',
    obs(1.00, 0.30, 195.0, 0.80, 0.20),
    [obs(1.40, 0.32, 196.0, 0.85, 0.20)])
ck("CALL demand", call_demand == iv.LIKELY_DIRECTIONAL_CALL_DEMAND)

put_demand = iv.classify_intent('PUT',
    obs(1.00, 0.30, 385.0, 0.20, 0.80),
    [obs(1.30, 0.32, 383.0, 0.20, 0.85)])
ck("PUT demand", put_demand == iv.LIKELY_DIRECTIONAL_PUT_DEMAND)

# ── Control TEST A — TSLA large put, premium fails, spot up, calls stronger ──
test_a = iv.classify_intent('PUT',
    obs(0.95, 0.30, 425.0, 0.80, 0.30),
    [obs(0.50, 0.28, 427.0, 0.85, 0.30)])
ck("TEST A -> PROBABLE_PUT_SUPPLY", test_a == iv.PROBABLE_PUT_SUPPLY)
ck("TEST A not directional (no PUT alert)", not iv.is_directional_demand(test_a))

# ── Control TEST E — call near high, premium+IV fall, spot against ──
test_e = iv.classify_intent('CALL',
    obs(1.00, 0.40, 200.0, 0.40, 0.70),
    [obs(0.60, 0.35, 199.0, 0.35, 0.75)])
ck("TEST E -> PROBABLE_CALL_SUPPLY", test_e == iv.PROBABLE_CALL_SUPPLY)
ck("TEST E not directional (research)", not iv.is_directional_demand(test_e))

# no follow-ups yet -> undecided
ck("no followups -> MIXED", iv.classify_intent('CALL', obs(1, 0.3, 195, 0.8, 0.2), []) == iv.MIXED_OR_UNKNOWN)

# ── opposite_side_veto (§9) ──
veto = iv.opposite_side_veto('PUT',
    obs(0.5, 0.28, 427.0, 0.90, 0.30, event_spot=425.0), prem_chg=-0.40)
ck("veto PUT when call dominates", veto == iv.VETO_OPPOSITE_DOMINANT)
noveto = iv.opposite_side_veto('PUT',
    obs(1.3, 0.32, 424.0, 0.40, 0.50, event_spot=425.0), prem_chg=0.20)
ck("no veto when put leads / thesis holds", noveto == '')

# ── IntentValidator lifecycle ──
config.INTENT_CONFIRMATION_BARS_MIN = 1
config.INTENT_CONFIRMATION_BARS_MAX = 3
v = iv.IntentValidator()
v.register('NVDA', 'CALL', 195.0, obs(1.00, 0.30, 195.0, 0.80, 0.20))
r1 = v.observe('NVDA', 'CALL', 195.0, obs(1.40, 0.32, 196.0, 0.85, 0.20))
ck("validator CONFIRMED on demand", r1['status'] == 'CONFIRMED' and iv.is_directional_demand(r1['intent_class']))

v.register('TSLA', 'PUT', 425.0, obs(0.95, 0.30, 425.0, 0.80, 0.30))
s = [v.observe('TSLA', 'PUT', 425.0, obs(0.50, 0.28, 427.0 + i, 0.85, 0.30)) for i in range(3)]
ck("validator PENDING before max", s[0]['status'] == 'PENDING')
ck("validator REJECTED at max (supply)", s[-1]['status'] == 'REJECTED')

# ── Gold-gate integration (flag ON) ──
config.GOLD_ONLY_PRODUCTION_MODE = True
def gsig(**kw):
    base = {'signal_context': 'PRIMARY_LEVEL_CONTINUATION', 'hv_pctile': 0.10,
            'low_dist': 1.10, 'signal_type': 'BULLISH', 'traded_strike': 195.0, 'upgrade': False}
    base.update(kw)
    gm.classify(base)
    return base

s_ok = gsig(intent_class=iv.LIKELY_DIRECTIONAL_CALL_DEMAND, opp_veto='')
ck("gate: GOLD + demand + no veto -> allowed", gm.production_allowed(s_ok) is True)
s_nointent = gsig(intent_class=None, opp_veto='')
ck("gate: no intent verdict -> blocked", gm.production_allowed(s_nointent) is False)
s_veto = gsig(intent_class=iv.LIKELY_DIRECTIONAL_CALL_DEMAND, opp_veto=iv.VETO_OPPOSITE_DOMINANT)
ck("gate: opposite-side veto -> blocked", gm.production_allowed(s_veto) is False)
s_rev = gsig(signal_context='PRIMARY_LEVEL_COUNTERTREND_REVERSAL', intent_class=None, opp_veto='')
ck("gate: reversal exempt from intent", gm.production_allowed(s_rev) is True)
config.GOLD_ONLY_PRODUCTION_MODE = False

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

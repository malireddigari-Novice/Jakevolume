"""
P2 live-wiring test — IntentGate deferred lifecycle. Run: python test_intent_gate.py

Verifies: a Gold candidate DEFERS on its event bar (not emitted); confirms to EMIT once
follow-up bars show directional demand; a supply case REJECTS to research; a chased
candidate is research immediately; a reversal emits immediately (intent-exempt).
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import intent_validation as iv
from analysis.intent_gate import IntentGate

config.INTENT_CONFIRMATION_BARS_MIN = 1
config.INTENT_CONFIRMATION_BARS_MAX = 2

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def O(mark, iv_, spot, cl, pl):
    return {'mark': mark, 'iv': iv_, 'spot': spot, 'call_leadership': cl, 'put_leadership': pl}

def gsig(side, ctx='PRIMARY_LEVEL_CONTINUATION', low=1.10):
    return {'signal_context': ctx, 'hv_pctile': 0.10, 'low_dist': low,
            'signal_type': 'BULLISH' if side == 'CALL' else 'BEARISH',
            'option_type': side, 'traded_strike': 195.0 if side == 'CALL' else 385.0,
            'upgrade': False}

gate = IntentGate()
cur = {}                                   # (side, strike) -> current obs
def obs_fn(side, strike):
    return cur[(side, float(strike))]

# ── Event bar: a gold call, a chased call, and a reversal put ──
call = gsig('CALL', low=1.10)
chased = gsig('CALL', low=2.10)
rev = gsig('PUT', ctx='PRIMARY_LEVEL_COUNTERTREND_REVERSAL', low=1.10)
cur[('CALL', 195.0)] = O(1.00, 0.30, 195.0, 0.80, 0.20)
cur[('PUT', 385.0)]  = O(1.00, 0.30, 385.0, 0.20, 0.80)
routed = gate.classify_new('NVDA', [call, chased, rev], obs_fn)
ck("gold call DEFERRED (not emitted)", call in routed['deferred'] and call not in routed['emit'])
ck("chased call -> research",          chased in routed['research'])
ck("reversal -> emit immediately",     rev in routed['emit'])

# ── Next bar: call premium expands + spot up + calls lead -> CONFIRMED emit ──
cur[('CALL', 195.0)] = O(1.45, 0.33, 196.0, 0.85, 0.20)
stepped = gate.step('NVDA', obs_fn)
ck("deferred call CONFIRMED -> emit", call in stepped['emit'])
ck("intent_class stamped demand",
   call.get('intent_class') == iv.LIKELY_DIRECTIONAL_CALL_DEMAND)

# ── Supply case: a gold put that fails (premium collapses, spot rises, calls lead) ──
put = gsig('PUT', low=1.10)
cur[('PUT', 385.0)] = O(1.00, 0.30, 385.0, 0.30, 0.80)
r2 = gate.classify_new('TSLA', [put], obs_fn)
ck("gold put DEFERRED", put in r2['deferred'])
# feed supply follow-ups until max
cur[('PUT', 385.0)] = O(0.40, 0.27, 388.0, 0.85, 0.30)
s1 = gate.step('TSLA', obs_fn)                    # n=1 < max -> pending
cur[('PUT', 385.0)] = O(0.30, 0.26, 389.0, 0.88, 0.30)
s2 = gate.step('TSLA', obs_fn)                    # n=2 == max -> rejected
ck("supply put not emitted",  put not in s1['emit'] and put not in s2['emit'])
ck("supply put -> research",  put in s2['research'])
ck("supply intent_class",     put.get('intent_class') == iv.PROBABLE_PUT_SUPPLY)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

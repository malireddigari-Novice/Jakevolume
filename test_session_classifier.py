"""Session classifier A/B/C test. Run: python test_session_classifier.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.session_classifier import (SessionClassifier, A_EXPANSION, B_POSITIONING,
                                         C_TRANSITION, UNDETERMINED)

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def expand(sc, sym, minutes=30):
    return sc.observe(sym, open_price=100.0, spot=101.5, session_high=101.6,
                      session_low=99.9, minutes_elapsed=minutes, lead_strength=0.8, lead_side='CALL')

def balanced(sc, sym, minutes=25):
    return sc.observe(sym, open_price=100.0, spot=100.1, session_high=100.3,
                      session_low=99.8, minutes_elapsed=minutes, lead_strength=0.3, lead_side=None)

# 1. Warm-up -> UNDETERMINED
sc = SessionClassifier()
r = sc.observe('AAA', open_price=100.0, spot=101.5, session_high=101.6, session_low=99.9,
               minutes_elapsed=10, lead_strength=0.8, lead_side='CALL')
ck("warm-up -> UNDETERMINED", r['type'] == UNDETERMINED)

# 2. Expansion off the open -> A
sc = SessionClassifier()
r = expand(sc, 'BBB')
ck(f"expansion + leadership -> A ({r['type']})", r['type'] == A_EXPANSION)
ck("A: directional (directionality high)", r['directionality'] >= 0.55)

# 3. Balanced chop -> B
sc = SessionClassifier()
r = balanced(sc, 'CCC')
ck(f"balanced + weak lead -> B ({r['type']})", r['type'] == B_POSITIONING)

# 4. Transition: B first, then expansion -> C
sc = SessionClassifier()
r1 = balanced(sc, 'DDD', minutes=25)
ck("DDD poll1 -> B", r1['type'] == B_POSITIONING)
r2 = expand(sc, 'DDD', minutes=40)
ck(f"DDD poll2 after positioning -> C ({r2['type']})", r2['type'] == C_TRANSITION)
ck("transition marked changed", r2['changed'] is True)

# 5. Straight expansion (no prior positioning) stays A, not C
sc = SessionClassifier()
expand(sc, 'EEE', minutes=30)
r = expand(sc, 'EEE', minutes=35)
ck("straight expansion stays A (not C)", r['type'] == A_EXPANSION and r['changed'] is False)

# 6. reset clears state
sc.reset('EEE')
ck("reset clears symbol", 'EEE' not in sc._state)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

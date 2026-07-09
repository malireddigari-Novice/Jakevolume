"""P5 §13 test — gate-by-gate audit. Run: python test_gate_audit.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import gold_mode

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def _reset(gold=True, intent=False, veto=False):
    config.GOLD_ONLY_PRODUCTION_MODE = gold
    config.INTENT_VALIDATION_ENABLED = intent
    config.OPPOSITE_SIDE_VETO_ENABLED = veto

def _gold_sig(**kw):
    s = {'gold_grade': 'GOLD', 'gold_subtype': gold_mode.GOLD_PRIMARY_LEVEL,
         'value_region': 'EXCELLENT_VALUE_REGION', 'clow_region': 'GOLD_VALUE_LOCATION'}
    s.update(kw)
    return s

# mode off -> pass-through, single SKIP gate
_reset(gold=False)
a = gold_mode.gate_audit(_gold_sig())
ck("mode off -> PRODUCTION", a['decision'] == 'PRODUCTION' and a['blocking_gate'] is None)
ck("mode off -> one SKIP gate", len(a['gates']) == 1 and a['gates'][0]['verdict'] == 'SKIP')

# clean GOLD, intent/veto off -> PRODUCTION
_reset()
a = gold_mode.gate_audit(_gold_sig())
ck("clean gold -> PRODUCTION", a['decision'] == 'PRODUCTION' and a['blocking_gate'] is None)
ck("clean gold -> intent SKIP", any(g['gate'] == 'INTENT' and g['verdict'] == 'SKIP' for g in a['gates']))

# research grade -> blocked at GRADE
_reset()
a = gold_mode.gate_audit(_gold_sig(gold_grade='RESEARCH'))
ck("research grade -> blocked GRADE", a['blocking_gate'] == 'GRADE' and a['decision'] == 'RESEARCH')

# unknown subtype -> blocked at SUBTYPE
_reset()
a = gold_mode.gate_audit(_gold_sig(gold_subtype='SOMETHING_ELSE'))
ck("bad subtype -> blocked SUBTYPE", a['blocking_gate'] == 'SUBTYPE')

# intent on, not demand -> blocked at INTENT
_reset(intent=True)
a = gold_mode.gate_audit(_gold_sig(intent_class='NO_CLEAR_INTENT'))
ck("intent fail -> blocked INTENT", a['blocking_gate'] == 'INTENT')

# veto on and vetoed -> blocked at OPP_VETO
_reset(veto=True)
a = gold_mode.gate_audit(_gold_sig(opp_veto=True))
ck("veto -> blocked OPP_VETO", a['blocking_gate'] == 'OPP_VETO')

# summary render
_reset(intent=True)
a = gold_mode.gate_audit(_gold_sig(intent_class='NO_CLEAR_INTENT'))
s = gold_mode.audit_summary(a)
ck("summary shows blocked-at", '→ RESEARCH (blocked at INTENT)' in s and 'INTENT:FAIL' in s)

# annotate_and_gate stamps gate_audit on sig
_reset()
sig = _gold_sig(hv_pctile=20, low_dist=1.0, signal_context='PRIMARY_LEVEL_CONTINUATION')
gold_mode.annotate_and_gate(sig)
ck("annotate stamps gate_audit", isinstance(sig.get('gate_audit'), dict) and 'gates' in sig['gate_audit'])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

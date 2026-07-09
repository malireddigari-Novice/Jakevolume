"""
P1 Gold-mode gate tests — value/contract-low region classifiers (§12/§13), subtype
classification (§1), and the production gate: OFF = pass-through (unchanged), ON =
Gold-only. Run:  python test_gold_mode.py   (exit 0 = pass)
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import gold_mode as gm

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1


# §12 historical-value regions
ck("value EXCELLENT",  gm.value_region(0.10) == 'EXCELLENT_VALUE_REGION')
ck("value ACCEPTABLE", gm.value_region(0.35) == 'ACCEPTABLE_VALUE_REGION')
ck("value NEUTRAL",    gm.value_region(0.55) == 'NEUTRAL_VALUE_REGION')
ck("value ELEVATED",   gm.value_region(0.80) == 'ELEVATED_VALUE_REGION')
ck("value None",       gm.value_region(None) is None)

# §13 contract-low-distance regions
ck("clow GOLD",       gm.contract_low_region(1.10) == 'GOLD_VALUE_LOCATION')
ck("clow STRONG",     gm.contract_low_region(1.40) == 'STRONG_VALUE_LOCATION')
ck("clow ACCEPTABLE", gm.contract_low_region(1.70) == 'ACCEPTABLE_ONLY_WITH_EXCEPTIONAL_EVIDENCE')
ck("clow CHASED",     gm.contract_low_region(2.00) == 'LIKELY_CHASED_OR_LATE')


def sig(**kw):
    base = {'signal_context': 'PRIMARY_LEVEL_CONTINUATION', 'hv_pctile': 0.10,
            'low_dist': 1.10, 'signal_type': 'BULLISH', 'traded_strike': 195.0,
            'upgrade': False}
    base.update(kw)
    return base


# §1 subtype classification
ck("subtype primary",     gm.classify(sig())['gold_subtype'] == gm.GOLD_PRIMARY_LEVEL)
ck("subtype chain-led",   gm.classify(sig(signal_context='CHAIN_LED_EMERGENT_ENTRY'))['gold_subtype'] == gm.GOLD_CHAIN_LED)
ck("subtype countertrend",gm.classify(sig(signal_context='PRIMARY_LEVEL_COUNTERTREND_REVERSAL'))['gold_subtype'] == gm.COUNTERTREND_REVERSAL)
ck("subtype upgrade",     gm.classify(sig(upgrade=True))['gold_subtype'] == gm.SAME_DIR_UPGRADE)

# grade: NVDA-195C-like (excellent value + gold low) -> GOLD; chased/elevated -> RESEARCH
ck("NVDA-like GOLD grade",   gm.classify(sig(hv_pctile=0.10, low_dist=1.10))['gold_grade'] == 'GOLD')
ck("chased RESEARCH grade",  gm.classify(sig(hv_pctile=0.80, low_dist=2.00))['gold_grade'] == 'RESEARCH')

# gate — OFF = pass-through (unchanged behavior)
config.GOLD_ONLY_PRODUCTION_MODE = False
s_gold = sig();                      gm.classify(s_gold)
s_res  = sig(hv_pctile=0.80, low_dist=2.0); gm.classify(s_res)
ck("OFF pass-through GOLD",     gm.production_allowed(s_gold) is True)
ck("OFF pass-through RESEARCH", gm.production_allowed(s_res) is True)

# gate — ON = Gold only. Scope to the P1 STRUCTURAL gate; P2's intent/veto have
# their own suite (test_intent_validation.py), so disable them here.
config.GOLD_ONLY_PRODUCTION_MODE = True
config.INTENT_VALIDATION_ENABLED = False
config.OPPOSITE_SIDE_VETO_ENABLED = False
ck("ON allows GOLD (structural)", gm.production_allowed(s_gold) is True)
ck("ON blocks RESEARCH",          gm.production_allowed(s_res) is False)
config.GOLD_ONLY_PRODUCTION_MODE = False
config.INTENT_VALIDATION_ENABLED = True
config.OPPOSITE_SIDE_VETO_ENABLED = True

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

"""
P3/P4 classification logic — opening story, countertrend-strict, superior-event.
Run: python test_classification.py
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.opening_scan import classify_opening_story
from analysis import countertrend as ct
from analysis import gold_mode as gm

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

# ── Opening story (§9/§11) ──
ck("TSLA-open: puts print, premium fades, spot up, calls lead -> PUT_SUPPLY_BULLISH",
   classify_opening_story(call_vol=2000, put_vol=3000, call_prem_chg=0.3, put_prem_chg=-0.4,
                          call_lead=0.8, put_lead=0.3, spot_chg=0.4) == 'OPENING_PUT_SUPPLY_BULLISH')
ck("calls demand -> CALL_DEMAND_DOMINANT",
   classify_opening_story(call_vol=2500, put_vol=200, call_prem_chg=0.5, put_prem_chg=-0.1,
                          call_lead=0.85, put_lead=0.2, spot_chg=0.5) == 'OPENING_CALL_DEMAND_DOMINANT')
ck("puts demand -> PUT_DEMAND_DOMINANT",
   classify_opening_story(call_vol=200, put_vol=2500, call_prem_chg=-0.1, put_prem_chg=0.5,
                          call_lead=0.2, put_lead=0.85, spot_chg=-0.5) == 'OPENING_PUT_DEMAND_DOMINANT')

# ── Countertrend (§11/§12) ──
ck("1% move from open -> established trend", ct.is_established_trend(431.25, 425.0) is True)
ck("0.3% move -> not established", ct.is_established_trend(426.3, 425.0) is False)
ck("NVDA/TSLA strict floors (1500/3000)", ct.countertrend_floors('TSLA') == (1500, 3000))
ck("standard strict floors (1250/2500)", ct.countertrend_floors('AAPL') == (1250, 2500))
ck("weak countertrend -> COUNTERTREND_WATCH (not gold)",
   ct.countertrend_label(symbol='TSLA', peak_1m=1300, vol_3m=1300, multi_or_exceptional=False,
                         prior_trend_faded=False, fresh_prior_conviction=True,
                         structure_reclaimed=False) == 'COUNTERTREND_WATCH')
ck("full confirmation -> GOLD_CONFIRMED_COUNTERTREND_REVERSAL",
   ct.countertrend_label(symbol='TSLA', peak_1m=2000, vol_3m=3200, multi_or_exceptional=True,
                         prior_trend_faded=True, fresh_prior_conviction=False,
                         structure_reclaimed=True) == 'GOLD_CONFIRMED_COUNTERTREND_REVERSAL')

# ── Superior same-direction event (§15) ──
good = dict(same_direction=True, stronger_volume=True, better_value=True, current_atm=True,
            activation_clear=True, thesis_valid=True, no_opposite_leadership_change=True)
ck("all-true -> improved timing qualifies", gm.improved_timing_qualifies(**good) is True)
ck("weaker volume -> no", gm.improved_timing_qualifies(**{**good, 'stronger_volume': False}) is False)
ck("thesis invalid -> no", gm.improved_timing_qualifies(**{**good, 'thesis_valid': False}) is False)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

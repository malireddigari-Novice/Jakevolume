"""
P6 CONTROL SUITE — the case-study acceptance gate (logic level).

Encodes the required regression controls against the built classification logic
(breakout, gold_mode gate, intent validation, Route B, countertrend, event-time). These
assert the correct CLASSIFICATION for each case; auto-firing them from the live detector
is the remaining integration, gated behind the Gold/event-time flags.

Run: python test_control_suite.py   (exit 0 = all controls green)
"""
import sys
from datetime import datetime
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import breakout as bo, gold_mode as gm, route_b, countertrend as ct
from analysis import intent_validation as iv
from analysis.rolling_volume import RollingVolume
from analysis.event_state import EventRegistry

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def _gold_sig(side, level_type, spot, level, bar_close, hv, low, intent, **extra):
    inter = bo.classify_interaction(side, level_type, spot, level, bar_close=bar_close)
    sig = {'signal_context': 'PRIMARY_LEVEL_CONTINUATION', 'hv_pctile': hv, 'low_dist': low,
           'signal_type': 'BULLISH' if side == 'CALL' else 'BEARISH', 'option_type': side,
           'traded_strike': level, 'upgrade': False, 'level_action': inter,
           'intent_class': intent, 'opp_veto': ''}
    sig.update(extra)
    gm.classify(sig)
    return sig, inter

config.GOLD_ONLY_PRODUCTION_MODE = True     # evaluate the production gate
config.INTENT_VALIDATION_ENABLED = True
config.OPPOSITE_SIDE_VETO_ENABLED = True

# ── TEST 1 — NVDA 195C positive control -> GOLD_PRIMARY_BOUNCE_CALL, tradeable ──
s1, i1 = _gold_sig('CALL', 'SUPPORT', 194.41, 195.0, 194.41, 0.04, 1.10,
                   iv.LIKELY_DIRECTIONAL_CALL_DEMAND)
ck("T1 NVDA195C interaction = BOUNCE_CALL", i1 == 'BOUNCE_CALL')
ck("T1 NVDA195C -> GOLD_PRIMARY_BOUNCE_CALL", s1['gold_subtype'] == gm.GOLD_PRIMARY_BOUNCE_CALL)
ck("T1 NVDA195C production-allowed", gm.production_allowed(s1) is True)

# ── TEST 2 — AAPL 310C accepted above R1 -> GOLD_PRIMARY_BREAKOUT_CALL ──
s2, i2 = _gold_sig('CALL', 'RESISTANCE', 310.4, 310.0, 310.4, 0.10, 1.20,
                   iv.LIKELY_DIRECTIONAL_CALL_DEMAND)
ck("T2 AAPL310C interaction = BREAKOUT_CALL", i2 == 'BREAKOUT_CALL')
ck("T2 AAPL310C -> GOLD_PRIMARY_BREAKOUT_CALL", s2['gold_subtype'] == gm.GOLD_PRIMARY_BREAKOUT_CALL)
ck("T2 AAPL310C production-allowed (call at resistance, accepted)", gm.production_allowed(s2) is True)

# ── TEST 3 — MSFT 385P 12:24 first countertrend print -> COUNTERTREND_WATCH ──
ck("T3 MSFT385P -> COUNTERTREND_WATCH (not entry)",
   ct.countertrend_label(symbol='MSFT', peak_1m=1685, vol_3m=1685, multi_or_exceptional=False,
                         prior_trend_faded=False, fresh_prior_conviction=True,
                         structure_reclaimed=False) == 'COUNTERTREND_WATCH')

# ── TEST 4 — MSFT 387.5P 15:11 -> IMPROVED_TIMING_AND_STRIKE ──
ck("T4 MSFT387.5P -> improved-timing upgrade qualifies",
   gm.improved_timing_qualifies(same_direction=True, stronger_volume=True, better_value=True,
                                current_atm=True, activation_clear=True, thesis_valid=True,
                                no_opposite_leadership_change=True) is True)

# ── TEST 5 — TSLA 425P @ 501 observed -> SUBTHRESHOLD, no production ──
rv = RollingVolume(); rv.observe_delta(501)
ck("T5 TSLA425P@501 fails production floor", rv.volume_pass(1000, 2000) is False)
reg = EventRegistry()
r5 = reg.observe('TSLA', 425.0, 'PUT', now=datetime(2026, 7, 2, 8, 30), spot=425.9,
                 atm_strike=425.0, r60=501, r180=501, floor_60=1000, floor_180=2000,
                 watch_vol=500, ttl_min=30)
ck("T5 TSLA425P@501 -> SUBTHRESHOLD_PARTIAL_EVENT", r5.no_retro_label() == 'SUBTHRESHOLD_PARTIAL_EVENT')

# ── TEST 6 — TSLA 425C @ 2.46K -> Route B exceptional single-strike ──
ck("T6 TSLA425C@2.46K -> Route B qualifies",
   route_b.route_b_qualifies(peak_1m=2460, strikes_from_atm=0, premium_notional=100000,
                             clow_region='STRONG_VALUE_LOCATION', concentrated=True,
                             opposite_dominates=False) is True)

# ── TEST 7 — TSLA 430P @ 2.34K post-extension -> GOLD_CONFIRMED_COUNTERTREND_REVERSAL ──
ck("T7 TSLA430P@2.34K -> GOLD_CONFIRMED_COUNTERTREND_REVERSAL",
   ct.countertrend_label(symbol='TSLA', peak_1m=2340, vol_3m=2340, multi_or_exceptional=True,
                         prior_trend_faded=True, fresh_prior_conviction=False,
                         structure_reclaimed=True) == 'GOLD_CONFIRMED_COUNTERTREND_REVERSAL')

# ── TEST 8 — MSFT 380C -> Route B (no primary-level dependency) ──
ck("T8 MSFT380C -> Route B qualifies (no primary proximity)",
   route_b.route_b_qualifies(peak_1m=2370, strikes_from_atm=1, premium_notional=50000,
                             clow_region='GOLD_VALUE_LOCATION', concentrated=True,
                             opposite_dominates=False) is True)

# ── Negative intent controls (TSLA false put / supply near high) ──
ck("N1 TSLA false put (fade + spot up + calls lead) -> PROBABLE_PUT_SUPPLY",
   iv.classify_intent('PUT', {'mark': 0.95, 'iv': 0.30, 'spot': 425.0, 'call_leadership': 0.8, 'put_leadership': 0.3},
                      [{'mark': 0.50, 'iv': 0.28, 'spot': 427.0, 'call_leadership': 0.85, 'put_leadership': 0.3}])
   == iv.PROBABLE_PUT_SUPPLY)

config.GOLD_ONLY_PRODUCTION_MODE = False
config.INTENT_VALIDATION_ENABLED = True
config.OPPOSITE_SIDE_VETO_ENABLED = True

print(f"\n=== {'ALL CONTROLS GREEN' if not fails else str(fails) + ' CONTROL(S) FAILED'} ===")
sys.exit(1 if fails else 0)

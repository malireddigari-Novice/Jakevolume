"""
P-BD test — primary-level interaction classifier + Gold subtype mapping.
Run: python test_breakout.py   (exit 0 = pass)

Centerpiece: AAPL 310C accepted above R1 310 -> BREAKOUT_CALL (must not be dropped
just because it is a call at resistance).
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import breakout as bo
from analysis import gold_mode as gm

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

# buffer: max(0.25, level*0.001); for 310 -> max(0.25, 0.31) = 0.31
ck("level_buffer(310) = 0.31", abs(bo.level_buffer(310.0) - 0.31) < 1e-9)

# classic same-side setups
ck("CALL @ support -> BOUNCE_CALL",
   bo.classify_interaction('CALL', 'SUPPORT', 305.2, 305.0) == 'BOUNCE_CALL')
ck("PUT @ resistance -> REJECTION_PUT",
   bo.classify_interaction('PUT', 'RESISTANCE', 309.8, 310.0) == 'REJECTION_PUT')

# AAPL 310C breakout: call at resistance, accepted above 310.31
ck("CALL @ resistance accepted above -> BREAKOUT_CALL",
   bo.classify_interaction('CALL', 'RESISTANCE', 310.4, 310.0, bar_close=310.4) == 'BREAKOUT_CALL')
# crossed but not accepted (310.1 < 310.31) -> FALSE_BREAKOUT
ck("CALL @ resistance not accepted -> FALSE_BREAKOUT",
   bo.classify_interaction('CALL', 'RESISTANCE', 310.1, 310.0, bar_close=310.1) == 'FALSE_BREAKOUT')

# breakdown: put at support accepted below (305-0.305 = 304.695)
ck("PUT @ support accepted below -> BREAKDOWN_PUT",
   bo.classify_interaction('PUT', 'SUPPORT', 304.5, 305.0, bar_close=304.5) == 'BREAKDOWN_PUT')
ck("PUT @ support not accepted -> FALSE_BREAKDOWN",
   bo.classify_interaction('PUT', 'SUPPORT', 304.9, 305.0, bar_close=304.9) == 'FALSE_BREAKDOWN')

# actionable set
ck("BREAKOUT_CALL actionable", bo.is_actionable('BREAKOUT_CALL'))
ck("FALSE_BREAKOUT not actionable", not bo.is_actionable('FALSE_BREAKOUT'))

# Gold subtype mapping via classify()
def sig(level_action):
    d = {'signal_context': 'PRIMARY_LEVEL_CONTINUATION', 'hv_pctile': 0.10, 'low_dist': 1.10,
         'signal_type': 'BULLISH', 'option_type': 'CALL', 'traded_strike': 310.0,
         'upgrade': False, 'level_action': level_action}
    gm.classify(d)
    return d

ck("breakout -> GOLD_PRIMARY_BREAKOUT_CALL subtype",
   sig('BREAKOUT_CALL')['gold_subtype'] == gm.GOLD_PRIMARY_BREAKOUT_CALL)
ck("bounce -> GOLD_PRIMARY_BOUNCE_CALL subtype",
   sig('BOUNCE_CALL')['gold_subtype'] == gm.GOLD_PRIMARY_BOUNCE_CALL)
ck("breakout subtype is a recognized Gold subtype",
   gm.GOLD_PRIMARY_BREAKOUT_CALL in gm._GOLD_SUBTYPES)

# ── level_side (detector side-selection by acceptance) ──
ck("resistance accepted above -> CALL breakout",
   bo.level_side('RESISTANCE', 310.4, 310.0, bar_close=310.4) == ('CALL', 'BULLISH', 'BREAKOUT_CALL'))
ck("resistance not crossed -> PUT rejection",
   bo.level_side('RESISTANCE', 309.7, 310.0, bar_close=309.7) == ('PUT', 'BEARISH', 'REJECTION_PUT'))
ck("resistance crossed-not-accepted -> None (skip)",
   bo.level_side('RESISTANCE', 310.1, 310.0, bar_close=310.1) is None)
ck("support accepted below -> PUT breakdown",
   bo.level_side('SUPPORT', 304.5, 305.0, bar_close=304.5) == ('PUT', 'BEARISH', 'BREAKDOWN_PUT'))
ck("support not crossed -> CALL bounce",
   bo.level_side('SUPPORT', 305.4, 305.0, bar_close=305.4) == ('CALL', 'BULLISH', 'BOUNCE_CALL'))
ck("support crossed-not-accepted -> None (skip)",
   bo.level_side('SUPPORT', 304.9, 305.0, bar_close=304.9) is None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

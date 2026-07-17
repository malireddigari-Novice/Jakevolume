"""
Alert-taxonomy tests. Run: python test_alert_taxonomy.py

Covers the Market State × Leadership Type derivation (all states + all leadership
types incl. gamma ramp), the trigger-reason list, and the unified Discord card
(single layout, and — critically — NO tiers / stars / gold-grade / confidence).
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from datetime import datetime
from analysis import alert_taxonomy as t

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def bars(seq):
    return [{'low': l, 'high': h, 'close': c} for (l, h, c) in seq]

FLAT = bars([(1, 1.1, 1.0)] * 12)

# ── Market state ───────────────────────────────────────────────────────────────
def st(sig, **kw):
    return t.classify(dict(sig), **kw)['market_state']

ck("reversal from countertrend shape",
   st({'signal_type': 'BULLISH', 'option_type': 'CALL', 'signal_shape': 'COUNTERTREND_REVERSAL',
       'level_label': 'S2'}, bars=FLAT) == t.REVERSAL)
ck("breakout from level_action",
   st({'signal_type': 'BEARISH', 'option_type': 'PUT', 'level_action': 'BREAKDOWN_PUT',
       'level_label': 'S1'}, bars=FLAT) == t.BREAKOUT)
exp = bars([(1, 1.1, 1.0)] * 5 + [(1, 1.4, 1.3), (1, 1.5, 1.45), (1, 1.7, 1.65), (1, 1.9, 1.85), (1, 2.1, 2.05)])
ck("trend expansion from widening+directional",
   st({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1'}, bars=exp) == t.TREND_EXPANSION)
comp = bars([(1, 2.0, 1.5)] * 5 + [(1, 1.1, 1.05)] * 5)
ck("compression from contracting range",
   st({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1'}, bars=comp) == t.COMPRESSION)
ck("transition is the default",
   st({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1'}, bars=FLAT) == t.TRANSITION)
ck("trend expansion via trend_dir when bars flat",
   st({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1'},
      bars=FLAT, trend_dir='BULLISH', trend_working=True) == t.TREND_EXPANSION)
# Precedence: reversal beats breakout.
ck("reversal precedence over breakout",
   st({'signal_type': 'BULLISH', 'option_type': 'CALL', 'signal_shape': 'REVERSAL',
       'level_action': 'BREAKOUT_CALL', 'level_label': 'R1'}, bars=FLAT) == t.REVERSAL)

# ── Leadership type ────────────────────────────────────────────────────────────
def ld(sig, **kw):
    return t.classify(dict(sig), **kw)['leadership_type']

ck("chain leader from context",
   ld({'signal_type': 'BULLISH', 'option_type': 'CALL', 'signal_context': 'CHAIN_LED_EMERGENT_ENTRY',
       'level_label': 'EMERGENT'}, bars=FLAT) == t.CHAIN_LEADER)
ck("primary level from rank label",
   ld({'signal_type': 'BULLISH', 'option_type': 'CALL', 'signal_context': 'PRIMARY_LEVEL_CONTINUATION',
       'level_label': 'R1'}, bars=FLAT) == t.PRIMARY_LEVEL)
ck("volume leader when no level label",
   ld({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': ''}, bars=FLAT) == t.VOLUME_LEADER)

accel = bars([(1, 1.1, 1.0)] * 3 + [(1, 1.2, 1.15), (1, 1.5, 1.45), (1, 1.9, 1.85)])
quotes = {(315.0, 'CALL'): {'gamma': 0.09}, (317.0, 'CALL'): {'gamma': 0.04}, (313.0, 'CALL'): {'gamma': 0.05}}
ck("gamma leader from accel + gamma peak",
   ld({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1', 'traded_strike': 315.0},
      bars=accel, quotes=quotes) == t.GAMMA_LEADER)
# Not a gamma ramp when price isn't accelerating (flat bars) — falls back to primary.
ck("no gamma ramp without acceleration",
   ld({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1', 'traded_strike': 315.0},
      bars=FLAT, quotes=quotes) == t.PRIMARY_LEVEL)
# Gamma disabled → never gamma leader.
config.GAMMA_LEADERSHIP_ENABLED = False
ck("gamma disabled -> not gamma leader",
   ld({'signal_type': 'BULLISH', 'option_type': 'CALL', 'level_label': 'S1', 'traded_strike': 315.0},
      bars=accel, quotes=quotes) == t.PRIMARY_LEVEL)
config.GAMMA_LEADERSHIP_ENABLED = True

# ── Reasons ────────────────────────────────────────────────────────────────────
s = {'signal_type': 'BULLISH', 'option_type': 'CALL', 'signal_context': 'CHAIN_LED_EMERGENT_ENTRY',
     'signal_shape': 'CHAIN_LED', 'level_label': 'EMERGENT', 'chain_strikes': [313, 315, 317],
     'trigger_volume': 508, 'premium_notional': 62000, 'level_type': 'SUPPORT', 'low_dist': 1.1}
r = t.classify(s, bars=FLAT)['trigger_reasons']
ck("reasons: chain leader bullet", any('chain leader' in x for x in r))
ck("reasons: no raw EMERGENT label", not any('EMERGENT' in x for x in r))
ck("reasons: volume + premium bullets", any('Volume' in x for x in r) and any('Premium' in x for x in r))

# ── Unified Discord card ───────────────────────────────────────────────────────
config.DISCORD_WEBHOOK_URL = 'http://x'
from output import discord_notifier as dn
cap = {}
dn._post = lambda url, payload: cap.clear() or cap.update(payload)

def card(sig):
    t.classify(sig, bars=FLAT)
    dn.send_signal(sig)
    return cap['embeds'][0]['description']

base = {'symbol': 'AAPL', 'signal_type': 'BULLISH', 'option_type': 'CALL', 'price_to_enter': 1.40,
        'traded_strike': 315.0, 'level_price': 315.0, 'level_label': 'S1', 'level_type': 'SUPPORT',
        'expiry': datetime(2026, 6, 9).date(), 'trigger_price': 315.2, 'trigger_volume': 508,
        'vol3m_window': 1240, 'atm_vol_1m': 508, 'premium_notional': 62000, 'low_dist': 1.18,
        'exit1_price': 317.0, 'exit2_price': 320.0, 'session_type': 'B_POSITIONING',
        'signal_context': 'PRIMARY_LEVEL_CONTINUATION', 'signal_time': datetime(2026, 7, 17, 10, 32, 15)}
primary_card = card(dict(base))

SECTIONS = ['**Market State**', '**Leadership**', '**Direction**', '**Why It Triggered**',
            '**Market Context**', '**Option Metrics**', '**Trade Plan**', '**System**']
for sec in SECTIONS:
    ck(f"card has section {sec}", sec in primary_card)

for banned in ['⭐', 'Gold', 'grade', 'GRADE', 'confidence', 'Confidence', 'WATCH', 'Tier', 'Strong', 'star']:
    ck(f"card omits '{banned}'", banned not in primary_card)

# A chain-led emergent alert renders through the SAME layout (same sections).
chain = dict(base)
chain.update(signal_context='CHAIN_LED_EMERGENT_ENTRY', signal_shape='CHAIN_LED',
             level_label='EMERGENT', chain_strikes=[313, 315, 317], emergent_spot=315.1)
chain_card = card(chain)
ck("chain-led uses identical section layout",
   all(sec in chain_card for sec in SECTIONS))
ck("chain-led shows Chain line", 'Chain:' in chain_card)
ck("both cards share the axis header format",
   '**Market State**' in chain_card and '**Leadership**' in chain_card)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

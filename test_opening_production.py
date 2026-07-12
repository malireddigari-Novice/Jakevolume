"""
Fix (2), Option C — opening event-time production promotion (default-off).

  1. opening_side_confirmed: only demand-dominant side is promotable (put-supply blocked).
  2. opening_story: frozen event-time ATM put with expanding premium + spot down + put
     leadership -> OPENING_PUT_DEMAND_DOMINANT.
  3. _chain_led_entry(force_strike=): anchors ATM to the forced (event-time) strike, not
     live proximity; a non-quoted force_strike -> OPENING_STRIKE_NOT_QUOTED.
  4. Flag ships OFF.

Run:  python test_opening_production.py   (exit 0 = pass)
"""
import sys
from datetime import date, datetime
from collections import deque

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis.signal_detector import SignalDetector
from analysis.opening_scan import opening_side_confirmed, opening_story
from analysis.event_state import EventRegistry
from data.market_utils import CST

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

# ── 1. directional gate ──
ck("CALL promotable only on call-demand",
   opening_side_confirmed('CALL', 'OPENING_CALL_DEMAND_DOMINANT') and
   not opening_side_confirmed('CALL', 'OPENING_PUT_DEMAND_DOMINANT'))
ck("PUT promotable only on put-demand",
   opening_side_confirmed('PUT', 'OPENING_PUT_DEMAND_DOMINANT') and
   not opening_side_confirmed('PUT', 'OPENING_PUT_SUPPLY_BULLISH'))
ck("no-conviction blocks both",
   not opening_side_confirmed('PUT', 'OPENING_NO_CONVICTION') and
   not opening_side_confirmed('CALL', 'OPENING_MIXED'))

# ── 2. opening_story from a frozen crossed EventState ──
reg = EventRegistry()
now = datetime(2026, 7, 9, 8, 40, tzinfo=CST)
# ATM put 202.5 crosses the floor at mark 1.00; spot 202 -> 200 (down); puts lead.
reg.observe('NVDA', 202.5, 'PUT', now=now, spot=200.0, atm_strike=202.5,
            r60=1200, r180=1800, floor_60=1000, floor_180=2000,
            bid=0.95, ask=1.05, last=1.00, watch_vol=500, ttl_min=30)
quotes = {(202.5, 'PUT'): {'mark': 1.30, 'bid': 1.25, 'ask': 1.35},   # premium expanded
          (202.5, 'CALL'): {'mark': 0.80},
          (200.0, 'PUT'): {'mark': 0.60}, (205.0, 'PUT'): {'mark': 2.0}}
story = opening_story('NVDA', quotes, reg, {'call_leadership': 0.1, 'put_leadership': 0.6},
                      close_price=200.0, spot_open=202.0)
ck(f"put-demand story ({story})", story == 'OPENING_PUT_DEMAND_DOMINANT')

# put SUPPLY (premium fading while spot rises, calls lead) must NOT be put-demand
reg2 = EventRegistry()
reg2.observe('NVDA', 202.5, 'PUT', now=now, spot=203.0, atm_strike=202.5,
             r60=1200, r180=1800, floor_60=1000, floor_180=2000,
             bid=1.15, ask=1.25, last=1.20, watch_vol=500, ttl_min=30)
q2 = {(202.5, 'PUT'): {'mark': 0.90}, (202.5, 'CALL'): {'mark': 1.5}}
story2 = opening_story('NVDA', q2, reg2, {'call_leadership': 0.6, 'put_leadership': 0.2},
                       close_price=203.5, spot_open=202.0)
ck(f"put-supply not promotable ({story2})",
   not opening_side_confirmed('PUT', story2))

# ── 3. _chain_led_entry force_strike anchoring ──
def _detector():
    d = SignalDetector(); d._history_date = date(2026, 7, 9); return d

def _setup(d, strikes_last5, ot='CALL', mark=1.55, low=1.2):
    odm, vd = {}, {}
    for k, last5 in strikes_last5.items():
        d._opt_vol_hist[('AMZN', k, ot)] = deque([20] * 15 + last5, maxlen=d._hist_maxlen)
        d._opt_mark_low[('AMZN', k, ot)] = low
        odm[(k, ot)] = {'mark': mark, 'day_low': low, 'bid': mark - 0.05, 'ask': mark + 0.05}
        vd[(k, ot)] = last5[-1]
    return odm, vd

_LEVELS = [{'level_type': 'RESISTANCE', 'rank': 1, 'strike': 240.0},
           {'level_type': 'SUPPORT', 'rank': 1, 'strike': 235.0}]
_BT = datetime(2026, 7, 9, 8, 40, tzinfo=CST)

d = _detector()
# spot 237.5 -> live ATM would be 237.5; force the event-time strike to 240 instead.
odm, vd = _setup(d, {235.0: [300, 350, 400, 380, 420],
                     237.5: [400, 500, 600, 520, 600],
                     240.0: [600, 700, 800, 760, 900]})
sig, reason = d._chain_led_entry('AMZN', 'CALL', odm, vd, _LEVELS, 237.5,
                                 date(2026, 7, 9), [{'close': 237.2}] * 7, _BT, _BT,
                                 False, None, None, force_strike=240.0)
ck(f"force_strike anchors ATM to 240 (traded={sig.get('traded_strike') if sig else None}, reason={reason})",
   sig is not None and abs(float(sig['traded_strike']) - 240.0) < 1e-6)

sig2, reason2 = d._chain_led_entry('AMZN', 'CALL', odm, vd, _LEVELS, 237.5,
                                   date(2026, 7, 9), [{'close': 237.2}] * 7, _BT, _BT,
                                   False, None, None, force_strike=999.0)
ck("non-quoted force_strike -> OPENING_STRIKE_NOT_QUOTED", reason2 == 'OPENING_STRIKE_NOT_QUOTED')

# regression: no force_strike still runs the live-ATM path (exact-strike selection,
# incl. the §7 1-OTM upgrade, is covered by test_chain_led). Anchor differs from the
# forced case: here ATM is live-proximity (237.5), which may then upgrade to a strong OTM.
sig3, _ = d._chain_led_entry('AMZN', 'CALL', odm, vd, _LEVELS, 237.5,
                             date(2026, 7, 9), [{'close': 237.2}] * 7, _BT, _BT,
                             False, None, None)
ck("no force_strike -> live-ATM path still yields a signal",
   sig3 is not None and sig3.get('option_type') == 'CALL')

# ── 4. ships dark ──
ck("OPENING_SCAN_PRODUCTION_ENABLED default off",
   config.OPENING_SCAN_PRODUCTION_ENABLED is False)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

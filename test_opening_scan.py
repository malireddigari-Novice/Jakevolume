"""
P-ET step 5 test — opening ATM±N event-time scan. Run: python test_opening_scan.py

The payoff: a contract that crossed the floor at the open stays eligible via its FROZEN
event-time distance even after spot runs several strikes away (TSLA-425P failure).
"""
import sys
from datetime import datetime, timedelta
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.event_state import EventRegistry
from analysis.opening_scan import scan_opening, event_time_eligible, strike_increment

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

t0 = datetime(2026, 7, 8, 8, 30, 0)
COMMON = dict(floor_60=1000, floor_180=2000, watch_vol=500, ttl_min=30)
STRIKES = (420.0, 422.5, 425.0, 427.5, 430.0)
oq = {(s, ot): {} for s in STRIKES for ot in ('CALL', 'PUT')}

ck("strike_increment = 2.5", strike_increment(oq) == 2.5)

reg = EventRegistry()
# 425C crosses at the open (ATM=425, dist 0)…
reg.observe('TSLA', 425.0, 'CALL', now=t0, spot=425.0, atm_strike=425.0, r60=1500, r180=1500, **COMMON)
# …then spot runs to 432.5 (ATM now 432.5) — frozen event ATM/dist must NOT change
reg.observe('TSLA', 425.0, 'CALL', now=t0 + timedelta(minutes=2), spot=432.5, atm_strike=432.5,
            r60=100, r180=100, **COMMON)
# 420C crossed but was 3 strikes from event ATM (spot/ATM 427.5)
reg.observe('TSLA', 420.0, 'CALL', now=t0, spot=427.5, atm_strike=427.5, r60=1500, r180=1500, **COMMON)
# 422.5P watched but never crossed the floor
reg.observe('TSLA', 422.5, 'PUT', now=t0, spot=425.0, atm_strike=425.0, r60=600, r180=600, **COMMON)

res = scan_opening('TSLA', oq, reg, window_strikes=5, increment=2.5)
occs = {(c['strike'], c['option_type']) for c in res}
ck("425C eligible via frozen event-time (spot ran to 432.5)", (425.0, 'CALL') in occs)
ck("420C eligible (event dist 3 <= window 5)", (420.0, 'CALL') in occs)
ck("422.5P excluded (watched, never crossed)", (422.5, 'PUT') not in occs)
ck("425C no_retro = QUALIFIED_AT_DECISION",
   next(c['no_retro'] for c in res if c['strike'] == 425.0) == 'QUALIFIED_AT_DECISION')

# tightening the window drops the far one but keeps the ATM one
res2 = scan_opening('TSLA', oq, reg, window_strikes=2, increment=2.5)
occs2 = {(c['strike'], c['option_type']) for c in res2}
ck("window=2: 425C kept", (425.0, 'CALL') in occs2)
ck("window=2: 420C (dist 3) dropped", (420.0, 'CALL') not in occs2)

# direct eligibility helper
es425 = reg.get('TSLA', 425.0, 'CALL')
ck("event_time_eligible True for crossed in-window", event_time_eligible(es425, 5, 2.5) is True)
ck("event_time_eligible None -> False", event_time_eligible(None, 5, 2.5) is False)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

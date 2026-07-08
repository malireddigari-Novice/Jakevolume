"""
P-ET step 2 test — EventRegistry lifecycle. Run: python test_event_state.py

Verifies: watch-cross freezes event-time ATM/spot/distance; the frozen values do NOT
move when spot runs away (the TSLA-425P fix); threshold-cross stamps decision state
once; TTL prune drops stale contracts.
"""
import sys
from datetime import datetime, timedelta
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.event_state import EventRegistry

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

reg = EventRegistry()
t0 = datetime(2026, 7, 8, 8, 30, 0)
COMMON = dict(floor_60=1000, floor_180=2000, watch_vol=500, ttl_min=30)

# Below watch → nothing registered
r = reg.observe('TSLA', 425.0, 'PUT', now=t0, spot=425.9, atm_strike=425.0, r60=300, r180=300, **COMMON)
ck("below watch -> None", r is None)

# WATCH cross (r60=600 >= 500): freeze event-time ATM=425, spot=425.9, distance 0
t1 = t0 + timedelta(seconds=30)
r = reg.observe('TSLA', 425.0, 'PUT', now=t1, spot=425.9, atm_strike=425.0,
                r60=600, r180=600, bid=2.3, ask=2.5, last=2.4, **COMMON)
ck("watch cross -> EventState", r is not None)
ck("frozen event ATM=425", r.atm_strike_at_event_start == 425.0)
ck("frozen event spot=425.9", r.spot_at_event_start == 425.9)
ck("event distance = 0", r.strike_distance_at_event == 0.0)
ck("not crossed yet (r60<1000, r180<2000)", r.crossed is False)

# THRESHOLD cross (r180=2100 >= 2000): stamp decision state
t2 = t1 + timedelta(minutes=1)
r = reg.observe('TSLA', 425.0, 'PUT', now=t2, spot=427.0, atm_strike=427.5,
                r60=800, r180=2100, bid=2.6, ask=2.8, last=2.7, **COMMON)
ck("threshold cross -> crossed", r.crossed is True)
ck("decision timestamp stamped", r.decision_timestamp == t2)
ck("observed_volume_at_decision = 800", r.observed_volume_at_decision == 800)
ck("threshold ask stamped", r.ask_at_threshold == 2.8)

# Spot runs away: contract now 2.5 strikes ITM (spot 431.25, ATM 431.25) — but the
# FROZEN event-time ATM/distance must NOT change (the whole point).
t3 = t2 + timedelta(minutes=2)
r = reg.observe('TSLA', 425.0, 'PUT', now=t3, spot=431.25, atm_strike=431.25,
                r60=100, r180=300, **COMMON)
ck("event ATM still 425 after spot ran away", r.atm_strike_at_event_start == 425.0)
ck("event distance still 0", r.strike_distance_at_event == 0.0)
ck("still eligible: 0 strikes from event ATM", r.strike_distance_strikes(2.5) == 0)
ck("evaluation-time would be 2.5 strikes away (431.25-425)/2.5",
   round(abs(431.25 - 425.0) / 2.5) == 2)   # eval-time distance the OLD logic would use

# TTL prune
t4 = t1 + timedelta(minutes=31)   # past ttl_min=30 from event_start (t1)
r = reg.observe('TSLA', 425.0, 'PUT', now=t4, spot=431.0, atm_strike=431.0,
                r60=100, r180=100, **COMMON)
ck("expired after TTL -> None", r is None)
ck("registry empty after prune", reg.get('TSLA', 425.0, 'PUT') is None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

"""P5 §17 test — signal latency profile. Run: python test_latency.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from datetime import datetime, timedelta
import pytz
from analysis.event_state import EventState

CST = pytz.timezone("America/Chicago")
fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def _es(**kw):
    base = dict(symbol="NVDA", strike=195.0, option_type="CALL",
                event_start_time=None, spot_at_event_start=195.0,
                atm_strike_at_event_start=195.0, strike_distance_at_event=0.0,
                ttl_expires_at=datetime(2026, 1, 1))
    base.update(kw)
    return EventState(**base)

t0 = CST.localize(datetime(2026, 7, 8, 8, 30, 0))
# watch at t0, threshold cross +45s, commit +50s
es = _es(event_start_time=t0)
es.threshold_cross_time = t0 + timedelta(seconds=45)
es.decision_timestamp = es.threshold_cross_time
es.commit_time = t0 + timedelta(seconds=50)
p = es.latency_profile()
ck("bar_wait 45s", p['bar_wait_secs'] == 45.0)
ck("commit_lag 5s", p['commit_lag_secs'] == 5.0)
ck("total 50s", p['total_latency_secs'] == 50.0)

# no commit yet -> commit-based segments None, bar_wait still computed
es2 = _es(event_start_time=t0)
es2.threshold_cross_time = t0 + timedelta(seconds=30)
es2.decision_timestamp = es2.threshold_cross_time
p2 = es2.latency_profile()
ck("no commit -> total None", p2['total_latency_secs'] is None)
ck("no commit -> commit_lag None", p2['commit_lag_secs'] is None)
ck("no commit -> bar_wait 30s", p2['bar_wait_secs'] == 30.0)

# never crossed -> bar_wait None
es3 = _es(event_start_time=t0)
p3 = es3.latency_profile()
ck("never crossed -> bar_wait None", p3['bar_wait_secs'] is None)

# tz-tolerant: naive commit vs aware event
es4 = _es(event_start_time=t0)
es4.threshold_cross_time = t0 + timedelta(seconds=20)
es4.decision_timestamp = es4.threshold_cross_time
es4.commit_time = (t0 + timedelta(seconds=25)).replace(tzinfo=None)
p4 = es4.latency_profile()
ck("tz-tolerant total 25s", p4['total_latency_secs'] == 25.0)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

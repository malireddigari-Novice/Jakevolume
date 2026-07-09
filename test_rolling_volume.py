"""P-ET step 1 test — RollingVolume default (bar-delta) backend. Run: python test_rolling_volume.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.rolling_volume import RollingVolume

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

rv = RollingVolume()
ck("empty r60=0",  rv.r60() == 0)
ck("empty r180=0", rv.r180() == 0)
ck("empty pass=False", rv.volume_pass(1000, 2000) is False)

for d in (100, 200, 300, 400, 500):
    rv.observe_delta(d)
ck("r60 = latest (500)",        rv.r60() == 500)
ck("r180 = sum last 3 (1200)",  rv.r180() == 300 + 400 + 500)
ck("peak_1m = current (500)",   rv.peak_1m() == 500)

# floor semantics — matches peak_1m>=1000 OR vol_3m>=2000
rv2 = RollingVolume()
for d in (50, 50, 1100):          # r60=1100 -> passes on 60s
    rv2.observe_delta(d)
ck("passes via r60",  rv2.volume_pass(1000, 2000) is True)
rv3 = RollingVolume()
for d in (700, 700, 700):         # r60=700<1000, r180=2100>=2000 -> passes on 180s
    rv3.observe_delta(d)
ck("passes via r180", rv3.volume_pass(1000, 2000) is True)
rv4 = RollingVolume()
for d in (800, 100, 900):         # r60=900<1000, r180=1800<2000 -> sub-threshold
    rv4.observe_delta(d)
ck("sub-threshold blocked", rv4.volume_pass(1000, 2000) is False)

# None-safe
rv5 = RollingVolume(); rv5.observe_delta(None)
ck("None delta -> 0", rv5.r60() == 0)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

"""P5 test — realistic paper fill + price-moved-from-event. Run: python test_paper_fill.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.paper_fill import executable_fill, price_moved_from_event

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

ck("ask present -> ASK_AT_COMMIT", executable_fill(0.95, 1.05, 1.0) == (1.05, 'ASK_AT_COMMIT'))
ck("no ask -> MARK_FALLBACK", executable_fill(0.95, None, 1.0) == (1.0, 'MARK_FALLBACK'))
ck("only bid -> BID_FALLBACK", executable_fill(0.90, None, None) == (0.90, 'BID_FALLBACK'))
ck("nothing -> NONE", executable_fill(None, None, None) == (None, 'NONE'))

# NVDA case: event printed ~0.92, commit fill 1.20 -> moved (~30% > 15% tol)
ck("NVDA 1.20 vs event 0.92 -> moved", price_moved_from_event(1.20, 0.92) is True)
ck("fill 1.02 vs event 0.98 -> not moved (4%)", price_moved_from_event(1.02, 0.98) is False)
ck("no event ref -> not moved", price_moved_from_event(1.20, None) is False)
ck("custom tol 0.05: 1.02 vs 0.98 -> moved", price_moved_from_event(1.02, 0.98, tol=0.03) is True)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

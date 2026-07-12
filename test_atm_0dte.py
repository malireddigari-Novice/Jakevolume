"""ATM 0DTE capture test. Run: python test_atm_0dte.py"""
import sys
from datetime import date
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.oi_levels import atm_0dte

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def ct(strike, bid, ask, mark):
    return {'strike': strike, 'bid': bid, 'ask': ask, 'mark': mark}

chain = {
    'expiry': date(2026, 7, 13),
    'calls': [ct(310, 4.9, 5.1, 5.0), ct(312.5, 3.4, 3.6, 3.5), ct(315, 2.3, 2.5, 2.4)],
    'puts':  [ct(310, 1.9, 2.1, 2.0), ct(312.5, 2.9, 3.1, 3.0), ct(315, 4.4, 4.6, 4.5)],
}
# spot 313 -> ATM = 312.5 both sides
a = atm_0dte(chain, 313.0)
ck("expiry carried", a['expiry'] == date(2026, 7, 13))
ck("ATM call strike 312.5", a['call']['strike'] == 312.5)
ck("ATM put strike 312.5", a['put']['strike'] == 312.5)
ck("call premium (mark/bid/ask)", a['call']['mark'] == 3.5 and a['call']['bid'] == 3.4 and a['call']['ask'] == 3.6)
ck("put premium", a['put']['mark'] == 3.0)

# spot 314.6 -> nearest is 315
a2 = atm_0dte(chain, 314.6)
ck("nearest picks 315 at spot 314.6", a2['call']['strike'] == 315 and a2['put']['strike'] == 315)

# missing put side
a3 = atm_0dte({'expiry': None, 'calls': [ct(100, 1, 1.2, 1.1)], 'puts': []}, 100.0)
ck("missing puts -> put None", a3['put'] is None and a3['call']['strike'] == 100)

# empty chain
a4 = atm_0dte({}, 100.0)
ck("empty chain -> both None", a4['call'] is None and a4['put'] is None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

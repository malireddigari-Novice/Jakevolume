"""ATM 0DTE window capture test (ATM + 1-OTM per side, premium + OI). Run: python test_atm_0dte.py"""
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

def ct(strike, bid, ask, mark, oi):
    return {'strike': strike, 'bid': bid, 'ask': ask, 'mark': mark, 'open_interest': oi}

chain = {
    'expiry': date(2026, 7, 13),
    'calls': [ct(310, 4.9, 5.1, 5.0, 500), ct(312.5, 3.4, 3.6, 3.5, 800),
              ct(315, 2.3, 2.5, 2.4, 1200), ct(317.5, 1.1, 1.3, 1.2, 900), ct(320, 0.5, 0.7, 0.6, 400)],
    'puts':  [ct(310, 1.9, 2.1, 2.0, 600), ct(312.5, 2.9, 3.1, 3.0, 1100),
              ct(315, 4.4, 4.6, 4.5, 1000), ct(317.5, 6.0, 6.2, 6.1, 300)],
}
# spot 314 -> ATM 315 both sides; OTM = 317.5C (above) and 312.5P (below)
a = atm_0dte(chain, 314.0, otm_steps=1)
ck("expiry carried", a['expiry'] == date(2026, 7, 13))
ck("call window = [315, 317.5]", [c['strike'] for c in a['call']] == [315.0, 317.5])
ck("put window = [315, 312.5]", [p['strike'] for p in a['put']] == [315.0, 312.5])
ck("ATM call premium+OI", a['call'][0]['mark'] == 2.4 and a['call'][0]['open_interest'] == 1200)
ck("1-OTM call (317.5C) premium+OI", a['call'][1]['mark'] == 1.2 and a['call'][1]['open_interest'] == 900)
ck("ATM put premium+OI", a['put'][0]['mark'] == 4.5 and a['put'][0]['open_interest'] == 1000)
ck("1-OTM put (312.5P) premium+OI", a['put'][1]['mark'] == 3.0 and a['put'][1]['open_interest'] == 1100)

# otm_steps=2 -> ATM + two OTM
a2 = atm_0dte(chain, 314.0, otm_steps=2)
ck("call window 3 deep = [315,317.5,320]", [c['strike'] for c in a2['call']] == [315.0, 317.5, 320.0])

# OTM clipped at chain edge (puts only go down to 310)
a3 = atm_0dte(chain, 314.0, otm_steps=5)
ck("put window clipped at edge", [p['strike'] for p in a3['put']] == [315.0, 312.5, 310.0])

# missing side
a4 = atm_0dte({'expiry': None, 'calls': [ct(100, 1, 1.2, 1.1, 50)], 'puts': []}, 100.0)
ck("missing puts -> empty list", a4['put'] == [] and a4['call'][0]['strike'] == 100)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

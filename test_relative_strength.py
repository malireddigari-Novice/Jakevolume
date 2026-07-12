"""Relative-strength module test. Run: python test_relative_strength.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.relative_strength import (pct_change, relative_strength, classify_rs,
                                        rs_tag, compute_row, divergences)

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

ck("pct_change +1%", pct_change(101.0, 100.0) == 1.0)
ck("pct_change None prev", pct_change(101.0, None) is None)
ck("pct_change zero prev", pct_change(101.0, 0) is None)

# NVDA +1.2%, QQQ +0.5% -> RS +0.7 (outperforming)
ck("RS outperform", relative_strength(1.2, 0.5) == 0.7)
ck("RS lag", relative_strength(-0.3, 0.5) == -0.8)
ck("RS None", relative_strength(None, 0.5) is None)

thr = 0.5
ck("strong classify", classify_rs(0.7, thr) == 'RELATIVELY_STRONG')
ck("weak classify", classify_rs(-0.8, thr) == 'RELATIVELY_WEAK')
ck("in-line classify", classify_rs(0.2, thr) == 'IN_LINE')
ck("unknown classify", classify_rs(None, thr) == 'UNKNOWN')
ck("boundary is strong (>=)", classify_rs(0.5, thr) == 'RELATIVELY_STRONG')
ck("tag strong", rs_tag(0.7, thr) == 'STRONG')

# compute_row: NVDA 202 from 200 prev = +1.0%, QQQ +0.3% -> RS +0.7 STRONG
row = compute_row('NVDA', 202.0, 200.0, 0.3, thr)
ck("compute_row pct", row['pct'] == 1.0)
ck("compute_row rs", row['rs'] == 0.7)
ck("compute_row class", row['rs_class'] == 'RELATIVELY_STRONG')

rows = [
    {'symbol': 'NVDA', 'rs': 0.9}, {'symbol': 'AAPL', 'rs': -0.6},
    {'symbol': 'MSFT', 'rs': 0.1}, {'symbol': 'TSLA', 'rs': -1.4},
    {'symbol': 'AMZN', 'rs': None},
]
d = divergences(rows, thr)
ck("divergences count (3 beyond 0.5)", len(d) == 3)
ck("divergences sorted by |rs|", [r['symbol'] for r in d] == ['TSLA', 'NVDA', 'AAPL'])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

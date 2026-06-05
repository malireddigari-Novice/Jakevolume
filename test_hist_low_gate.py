"""
Deterministic tests for the historical-low entry gate.
Run: python test_hist_low_gate.py   (no DB or network needed)

The gate requires an actionable entry to be trading at/near the contract's
multi-day historical low (mark / hist_low <= HIST_LOW_NEAR_RATIO). A failing
entry is downgraded to a WATCH alert (still surfaced, never auto-traded). When
no history is available (hist_low_fn None, or 0DTE returning None) the gate is a
no-op and the entry is preserved.
"""
from datetime import date, datetime, timedelta

import pytz

import config
from analysis.signal_detector import SignalDetector

CST = pytz.timezone('America/Chicago')
_fail = 0

# 0DTE expiry == bar date keeps frozen-role behaviour while still letting the
# detector build an OCC symbol for the gate lookup.
EXPIRY = date(2099, 3, 2)


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


def _q(mark, vol, spread=0.02):
    return {'bid': mark - spread / 2, 'ask': mark + spread / 2, 'mark': mark,
            'volume': vol, 'open_interest': 1000, 'day_high': 1.0, 'day_low': 0.4}


def _bar(i):
    t = CST.localize(datetime(2099, 3, 2, 8, 30) + timedelta(minutes=i))
    return {'bar_time': t, 'open': 100.0, 'high': 100.0, 'low': 100.0,
            'close': 100.0, 'volume': 1000}


LEVELS = [
    {'level_type': 'SUPPORT',    'rank': 2, 'strike': 100.0, 'option_type': 'CALL', 'open_interest': 1000},
    {'level_type': 'RESISTANCE', 'rank': 1, 'strike': 101.0, 'option_type': 'PUT',  'open_interest': 1000},
]

# ATM single fat print, ITM quiet → EXTREME_SINGLE_PRINT / MEDIUM_HIGH (actionable).
D_ATM = [10] * 10 + [500]
D_ITM = [10] * 10 + [10]


def run(hist_low_fn):
    det = SignalDetector()
    atm_cum = itm_cum = 0
    last = []
    for i, (da, di) in enumerate(zip(D_ATM, D_ITM)):
        atm_cum += da
        itm_cum += di
        quotes = {(100.0, 'CALL'): _q(0.5, atm_cum),
                  (99.0,  'CALL'): _q(0.5, itm_cum)}
        sig = det.check('AAPL', [_bar(i)], LEVELS, quotes, expiry=EXPIRY,
                        pc_ratio=None, hist_low_fn=hist_low_fn)
        if sig:
            last = sig
    return last


# Baseline: no fn → gate inert, entry stays actionable MEDIUM_HIGH.
base = run(None)
check("baseline: actionable entry fires", len(base) == 1)
if base:
    check("baseline: MEDIUM_HIGH", base[0]['confidence'] == 'MEDIUM_HIGH')

# Near the historical low (mark 0.5 / low 0.45 = 1.11 <= 1.25) → preserved.
near = run(lambda occ: 0.45)
check("near hist low: entry preserved actionable", len(near) == 1)
if near:
    check("near hist low: still MEDIUM_HIGH", near[0]['confidence'] == 'MEDIUM_HIGH')

# Far above the historical low (0.5 / 0.2 = 2.5 > 1.25) → downgraded to WATCH.
far = run(lambda occ: 0.20)
check("far above hist low: still emitted (WATCH)", len(far) == 1)
if far:
    check("far above hist low: downgraded to WATCH", far[0]['confidence'] == 'WATCH')

# No history (fn returns None, e.g. 0DTE) → gate not applicable, entry preserved.
none_hist = run(lambda occ: None)
check("no history: entry preserved actionable", len(none_hist) == 1)
if none_hist:
    check("no history: still MEDIUM_HIGH", none_hist[0]['confidence'] == 'MEDIUM_HIGH')

# Gate disabled by config → fn ignored, entry preserved even when far above low.
config.HIST_LOW_ENTRY_GATE = False
try:
    disabled = run(lambda occ: 0.20)
finally:
    config.HIST_LOW_ENTRY_GATE = True
check("gate disabled: entry preserved actionable", len(disabled) == 1)
if disabled:
    check("gate disabled: still MEDIUM_HIGH", disabled[0]['confidence'] == 'MEDIUM_HIGH')

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
raise SystemExit(1 if _fail else 0)

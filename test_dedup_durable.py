"""
Deterministic tests for the durable (DB-backed) one-call/one-put-per-day dedup.
Run: python test_dedup_durable.py   (no DB or network needed)

A restarted or second concurrent process loses the in-memory _fired_today, so it
would re-fire a direction already alerted earlier that day. fired_today_fn folds
the directions already in the DB back into the dedup state each bar:
  - same/lower confidence already fired  -> skip (no duplicate)
  - only a WATCH already fired            -> an actionable entry still fires
"""
from datetime import datetime, timedelta

import pytz

from analysis.signal_detector import SignalDetector

CST = pytz.timezone('America/Chicago')
_fail = 0


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

# ATM single fat print -> BULLISH MEDIUM_HIGH / EXTREME_SINGLE_PRINT (actionable).
D_ATM = [10] * 10 + [500]
D_ITM = [10] * 10 + [10]


def run(fired_today_fn):
    det = SignalDetector()
    atm_cum = itm_cum = 0
    last = []
    for i, (da, di) in enumerate(zip(D_ATM, D_ITM)):
        atm_cum += da
        itm_cum += di
        quotes = {(100.0, 'CALL'): _q(0.5, atm_cum),
                  (99.0,  'CALL'): _q(0.5, itm_cum)}
        sig = det.check('AAPL', [_bar(i)], LEVELS, quotes, expiry=None,
                        pc_ratio=None, fired_today_fn=fired_today_fn)
        if sig:
            last = sig
    return last


# Baseline: nothing fired yet -> entry fires.
base = run(lambda sym, day: {})
check("baseline: fires when DB empty", len(base) == 1 and base[0]['confidence'] == 'MEDIUM_HIGH')

# Restart/concurrent: same direction already at MEDIUM_HIGH -> no duplicate.
same = run(lambda sym, day: {'BULLISH': ['MEDIUM_HIGH']})
check("equal confidence already fired -> skipped (no dup)", len(same) == 0)

# A stronger HIGH already fired -> still skipped.
stronger = run(lambda sym, day: {'BULLISH': ['HIGH']})
check("stronger already fired -> skipped", len(stronger) == 0)

# Only a WATCH fired earlier -> the real actionable entry still fires.
after_watch = run(lambda sym, day: {'BULLISH': ['WATCH']})
check("prior WATCH -> actionable entry still fires", len(after_watch) == 1)

# Opposite direction already fired must not block this one.
other_dir = run(lambda sym, day: {'BEARISH': ['HIGH']})
check("opposite direction fired -> this direction still fires", len(other_dir) == 1)

# A failing fired_today_fn must not crash detection (falls back to in-memory).
def _boom(sym, day):
    raise RuntimeError("db down")
resilient = run(_boom)
check("fired_today_fn error -> detection still works", len(resilient) == 1)

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
raise SystemExit(1 if _fail else 0)

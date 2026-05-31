"""
Tests for next-day-expiry (Tue/Thu) mode: interchangeable levels + OTM target
strike. Run: python test_next_day_mode.py  (no DB/network).
"""
from datetime import datetime, timedelta

import pytz

import config
from analysis.signal_detector import SignalDetector, _opposing_strikes, _otm_target_contract

CST = pytz.timezone('America/Chicago')
_fail = 0


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


# Levels: a deep support at 100, with 102/104 above. (rank order S3,S2,S1)
LEVELS = [
    {'level_type': 'SUPPORT', 'rank': 3, 'strike': 100.0, 'option_type': 'CALL', 'open_interest': 1000},
    {'level_type': 'SUPPORT', 'rank': 2, 'strike': 102.0, 'option_type': 'CALL', 'open_interest': 1000},
    {'level_type': 'SUPPORT', 'rank': 1, 'strike': 104.0, 'option_type': 'CALL', 'open_interest': 1000},
]

# ── Unit: interchangeable levels by spot position ─────────────────────────────
above, below = _opposing_strikes(LEVELS, spot=100.0, position_only=True)
check("flip: 102/104 act as resistance above spot", sorted(above) == [102.0, 104.0])
check("flip: level at spot 100 counts as support", below == [100.0])
above, below = _opposing_strikes(LEVELS, spot=103.0, position_only=True)
check("flip: spot 103 -> 104 resistance, 100/102 support",
      above == [104.0] and sorted(below) == [100.0, 102.0])

# ── Unit: OTM target strike picks the next level up ───────────────────────────
chain = {(s, 'CALL'): {'bid': 0.4, 'ask': 0.5, 'mark': 0.45}
         for s in (99.0, 100.0, 101.0, 102.0, 103.0)}
tgt, otm, data = _otm_target_contract('BULLISH', 'CALL', spot=100.0, levels=LEVELS,
                                      chain_quotes=chain, depth=1)
check("OTM: bullish at S3(100) targets next level up 102", tgt == 102.0)
check("OTM: strike nearest target = 102", otm == 102.0)


# ── Integration: next-day cluster fires with OTM strike ───────────────────────
def _q(mark, vol, spread=0.02):
    return {'bid': mark - spread / 2, 'ask': mark + spread / 2, 'mark': mark,
            'volume': vol, 'open_interest': 1000, 'day_high': 1.0, 'day_low': 0.4}


def _bar(i):
    return {'bar_time': CST.localize(datetime(2099, 3, 3, 8, 30) + timedelta(minutes=i)),
            'open': 100.0, 'high': 100.0, 'low': 100.0, 'close': 100.0, 'volume': 1000}


def run(expiry, with_chain):
    det = SignalDetector()
    a = t = 0
    last = []
    full_chain = {(s, 'CALL'): _q(0.5, 0) for s in (99.0, 100.0, 101.0, 102.0, 103.0)}
    for i, d in enumerate([10] * 10 + [35] * 5):
        a += d; t += d
        watched = {(100.0, 'CALL'): _q(0.5, a), (99.0, 'CALL'): _q(0.5, t)}
        cq = dict(full_chain) if with_chain else None
        if cq:                      # keep the spot-side cumulative volume in the chain too
            cq[(100.0, 'CALL')] = _q(0.5, a)
            cq[(99.0, 'CALL')] = _q(0.5, t)
        sig = det.check('AAPL', [_bar(i)], LEVELS, watched, expiry=expiry,
                        pc_ratio=None, chain_quotes=cq)
        if sig:
            last = sig
    return last[0] if last else None


TODAY = _bar(0)['bar_time'].date()

# Next-day: expiry tomorrow → flip + OTM strike at 102
s = run(expiry=TODAY + timedelta(days=1), with_chain=True)
check("next-day: a signal fires", s is not None)
if s:
    check("next-day: day_mode NEXT_DAY", s['day_mode'] == 'NEXT_DAY')
    check("next-day: signal BULLISH / CALL (S3 is support)",
          s['signal_type'] == 'BULLISH' and s['option_type'] == 'CALL')
    check("next-day: level_price stays 100 (detection level)", s['level_price'] == 100.0)
    check("next-day: traded_strike = OTM target 102", s['traded_strike'] == 102.0)
    check("next-day: target_level = 102", s['target_level'] == 102.0)

# 0DTE: expiry == today → unchanged (traded_strike = level, no target)
s0 = run(expiry=TODAY, with_chain=False)
check("0DTE: a signal fires", s0 is not None)
if s0:
    check("0DTE: day_mode 0DTE", s0['day_mode'] == '0DTE')
    check("0DTE: traded_strike = level price 100", s0['traded_strike'] == 100.0)
    check("0DTE: no target_level", s0['target_level'] is None)

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
raise SystemExit(1 if _fail else 0)

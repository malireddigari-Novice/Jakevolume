"""Chandelier trailing-exit test. Run: python test_chandelier.py"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis import chandelier as ch

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def bar(h, l, c):
    return {'high': h, 'low': l, 'close': c}

# true range
ck("TR simple range", ch.true_range(101, 99, 100) == 2)
ck("TR gap up (prev_close below low)", ch.true_range(105, 103, 100) == 5)

# ATR: 15 bars of 2-wide ranges (period 14) -> ATR = 2
bars = [bar(100 + i, 98 + i, 99 + i) for i in range(15)]
a = ch.atr(bars, 14)
ck("ATR ~2 on steady 2-wide bars", a is not None and abs(a - 2.0) < 0.5)
ck("ATR None when too few bars", ch.atr(bars[:5], 14) is None)

# running extreme
ck("BULLISH extreme = max high", ch.running_extreme(bars, 'BULLISH') == 100 + 14)
ck("BEARISH extreme = min low", ch.running_extreme(bars, 'BEARISH') == 98)

# chandelier stop
ck("BULLISH stop = ext - atr*mult", ch.chandelier_stop('BULLISH', 120, 2, 3) == 114.0)
ck("BEARISH stop = ext + atr*mult", ch.chandelier_stop('BEARISH', 100, 2, 3) == 106.0)
ck("stop None when atr None", ch.chandelier_stop('BULLISH', 120, None, 3) is None)

# evaluate — BULLISH: rallied to 114 (ext), ATR 2, mult 3 -> stop 108.
rally = [bar(100 + i, 99 + i, 100 + i) for i in range(15)]   # highs 100..114, ATR ~1
ev = ch.evaluate('BULLISH', 113.0, rally, period=14, mult=3.0)
ck("BULLISH ready", ev['ready'] is True)
ck("BULLISH holds above stop (no exit)", ev['exit'] is False and 113.0 > ev['stop'])
ev2 = ch.evaluate('BULLISH', ev['stop'] - 0.5, rally, period=14, mult=3.0)
ck("BULLISH exits when spot below stop", ev2['exit'] is True)

# not ready with too few bars -> never exits blind
ev3 = ch.evaluate('BULLISH', 50.0, rally[:3], period=14, mult=3.0)
ck("not ready -> no blind exit", ev3['ready'] is False and ev3['exit'] is False)

# BEARISH mirror: falling market, stop above; exit when spot rises through it
fall = [bar(101 - i, 99 - i, 100 - i) for i in range(15)]    # lows 99..85
evb = ch.evaluate('BEARISH', 88.0, fall, period=14, mult=3.0)
ck("BEARISH ready", evb['ready'] is True)
ck("BEARISH exits when spot above stop", ch.evaluate('BEARISH', evb['stop'] + 0.5, fall, period=14, mult=3.0)['exit'] is True)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)

"""
Tests for the loophole fixes: per-level cooldown (#3), watched-strike
discontinuity reset (#4), true-day-low chase guard (#5), next-day flip
deadband (#7). Run: python test_loophole_fixes.py  (no DB/network).
"""
from datetime import datetime, timedelta

import pytz

import config
from analysis import signal_detector as sd
from analysis.signal_detector import SignalDetector, _CONF_RANK

CST = pytz.timezone('America/Chicago')
_fail = 0


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


T0 = CST.localize(datetime(2099, 4, 7, 9, 0))


def _sig(strike, conf, sigtype='BULLISH'):
    return {'symbol': 'X', 'signal_type': sigtype, 'level_price': strike, 'confidence': conf}


# ── #3 Per-level cooldown + upgrade ───────────────────────────────────────────
det = SignalDetector()
cd = config.SIGNAL_COOLDOWN_MINUTES
check("cooldown: first sight fires", det._fire_decision(_sig(100.0, 'MEDIUM_HIGH'), T0) == ('fire', False))

det._last_fired[('X', 'BULLISH', 100.0)] = (T0, _CONF_RANK['MEDIUM_HIGH'])
check("cooldown: same level same tier within cooldown skips",
      det._fire_decision(_sig(100.0, 'MEDIUM_HIGH'), T0 + timedelta(minutes=5)) == ('skip', False))
check("cooldown: higher tier within cooldown upgrades",
      det._fire_decision(_sig(100.0, 'HIGH'), T0 + timedelta(minutes=5)) == ('upgrade', True))
check("cooldown: re-entry after cooldown fires",
      det._fire_decision(_sig(100.0, 'MEDIUM_HIGH'), T0 + timedelta(minutes=cd + 1)) == ('fire', False))
check("cooldown: a different level fires independently",
      det._fire_decision(_sig(102.0, 'MEDIUM_HIGH'), T0 + timedelta(minutes=5)) == ('fire', False))

# watch then actionable = fresh entry, NOT an upgrade (so the trade isn't skipped)
det._last_fired[('X', 'BULLISH', 104.0)] = (T0, _CONF_RANK['WATCH'])
check("cooldown: actionable over prior WATCH is a fresh fire",
      det._fire_decision(_sig(104.0, 'MEDIUM'), T0 + timedelta(minutes=5)) == ('fire', False))


# ── #5 chase guard uses the true day low ──────────────────────────────────────
det2 = SignalDetector()
okey = ('X', 100.0, 'CALL')
det2._opt_mark_low[okey] = 0.50                       # watched-session min (stale, high)
ld = det2._contract_low_dist(okey, {'mark': 0.60, 'day_low': 0.20})
check("low_dist uses true day_low (0.60/0.20=3.0)", ld == 3.0)
ld2 = det2._contract_low_dist(okey, {'mark': 0.60})   # no day_low → watched min
check("low_dist falls back to watched min (0.60/0.50=1.2)", ld2 == 1.2)


# ── #4 watched-strike discontinuity reset ─────────────────────────────────────
def _q(vol, mark=0.5):
    return {'bid': mark - 0.01, 'ask': mark + 0.01, 'mark': mark, 'volume': vol,
            'open_interest': 1000, 'day_high': 1.0, 'day_low': 0.4}


LEVELS_FAR = [{'level_type': 'SUPPORT', 'rank': 1, 'strike': 50.0, 'option_type': 'CALL'}]


def _barx(dt):
    return {'bar_time': dt, 'open': 100.0, 'high': 100.0, 'low': 100.0, 'close': 100.0, 'volume': 1}


det3 = SignalDetector()
key = ('X', 100.0, 'CALL')
# bar0 first sight (delta 0), bar1 +10 (delta 10), bar2 after a 2-min GAP with a
# huge cumulative jump → must reset to delta 0 (not 4990) and clear history.
det3.check('X', [_barx(T0)],                          LEVELS_FAR, {(100.0, 'CALL'): _q(100)})
det3.check('X', [_barx(T0 + timedelta(seconds=60))],  LEVELS_FAR, {(100.0, 'CALL'): _q(110)})
det3.check('X', [_barx(T0 + timedelta(seconds=180))], LEVELS_FAR, {(100.0, 'CALL'): _q(5000)})
hist = list(det3._opt_vol_hist[key])
check("discontinuity: gap re-entry resets delta to 0 (no fake spike)", hist[-1] == 0)
check("discontinuity: history cleared on gap", hist == [0])
# continuous next bar resumes normal deltas
det3.check('X', [_barx(T0 + timedelta(seconds=240))], LEVELS_FAR, {(100.0, 'CALL'): _q(5050)})
check("discontinuity: normal delta resumes after reset", list(det3._opt_vol_hist[key])[-1] == 50)


# ── #7 next-day flip deadband ─────────────────────────────────────────────────
# Pivot level @100 stored RESISTANCE, with 98 (support) and 102 (resistance) for
# targets. Cluster volume on BOTH call and put sides so whichever role wins fires.
PIVOT_LEVELS = [
    {'level_type': 'RESISTANCE', 'rank': 2, 'strike': 100.0, 'option_type': 'PUT'},
    {'level_type': 'SUPPORT',    'rank': 3, 'strike': 98.0,  'option_type': 'CALL'},
    {'level_type': 'RESISTANCE', 'rank': 1, 'strike': 102.0, 'option_type': 'PUT'},
]
EXP = T0.date() + timedelta(days=1)


def run_flip(close):
    det = SignalDetector()
    a = 0
    last = None
    for i, d in enumerate([10] * 10 + [35] * 5):
        a += d
        t = CST.localize(datetime(2099, 4, 7, 9, 0)) + timedelta(minutes=i)
        bar = {'bar_time': t, 'open': close, 'high': close, 'low': close, 'close': close, 'volume': 1}
        watched = {(100.0, 'CALL'): _q(a), (99.0, 'CALL'): _q(a),
                   (100.0, 'PUT'): _q(a),  (101.0, 'PUT'): _q(a)}
        chain = {k: _q(0) for k in [(98.0, 'PUT'), (102.0, 'CALL'), (102.0, 'PUT'), (98.0, 'CALL')]}
        chain.update(watched)
        r = det.check('X', [bar], PIVOT_LEVELS, watched, expiry=EXP, pc_ratio=None, chain_quotes=chain)
        if r:
            last = r[0]
    return last


band = 100.0 * config.LEVEL_FLIP_DEADBAND_PCT
inside = run_flip(100.0 + band / 2)     # within deadband → keep stored RESISTANCE
check("deadband: within band keeps stored role (BEARISH)",
      inside is not None and inside['signal_type'] == 'BEARISH' and inside['level_type'] == 'RESISTANCE')
above = run_flip(100.0 + band * 3)      # clearly above strike → flips to SUPPORT
check("deadband: clearly above strike flips to support (BULLISH)",
      above is not None and above['signal_type'] == 'BULLISH' and above['level_type'] == 'SUPPORT')

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
raise SystemExit(1 if _fail else 0)

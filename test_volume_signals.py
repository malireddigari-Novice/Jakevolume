"""
Deterministic tests for the spec single-print / cluster logic.
Run: python test_volume_signals.py   (no DB or network needed)
"""
from datetime import datetime, timedelta

import pytz

import config
from analysis import signal_detector as sd
from analysis.signal_detector import SignalDetector, _avg_prior, _single_print, _cluster5, _spread_pct

CST = pytz.timezone('America/Chicago')
_fail = 0


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


# ── Pure helpers ──────────────────────────────────────────────────────────────
check("avg_prior excludes current bar",
      _avg_prior([10] * 9 + [999], exclude_last=1, lookback=10) == 10.0)
check("avg_prior empty -> 0.0",
      _avg_prior([1, 2, 3], exclude_last=5, lookback=10) == 0.0)

# Single print: prior10 avg = 10 -> base 10
v, r = _single_print([10] * 10 + [400], 400, 300)
check("single print valid (ratio 40x, >=floor 300)", v and r == 40.0)
v, _ = _single_print([10] * 10 + [200], 200, 300)
check("single print fails volume floor (200<300)", not v)
v, r = _single_print([100] * 10 + [400], 400, 300)
check("single print fails ratio (4x<8x)", not v and r == 4.0)

# Cluster: base_unit from the 10 bars before the 5-bar window
c = _cluster5([10] * 10 + [40, 40, 40, 40, 5])
check("cluster valid: ratio>=3 AND active>=3",
      c['valid_core'] and c['active'] == 4 and c['ratio'] == 165 / 50)
c = _cluster5([10] * 10 + [200, 5, 5, 5, 5])
check("ONE fat bar is NOT a cluster (active=1<3)",
      (not c['valid_core']) and c['active'] == 1)
c = _cluster5([10] * 10 + [40, 5, 40, 5, 40])
check("active>=3 but window ratio<3 -> not cluster",
      (not c['valid_core']) and c['active'] == 3 and c['ratio'] < 3.0)
c = _cluster5([10] * 3)
check("too few bars -> not a cluster", not c['valid_core'])

check("spread pct", abs(_spread_pct({'bid': 1.0, 'ask': 1.5}) - 0.4) < 1e-9)
check("spread pct None when missing", _spread_pct({'bid': 1.0}) is None)


# ── Integration: drive detector.check across bars ─────────────────────────────
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


def run(deltas_atm, deltas_itm, itm_present=True, collect=False):
    """Feed cumulative-volume bars; return last signal list (or all if collect)."""
    det = SignalDetector()
    atm_cum = itm_cum = 0
    last, all_sigs = [], []
    for i, (da, di) in enumerate(zip(deltas_atm, deltas_itm)):
        atm_cum += da
        itm_cum += di
        quotes = {(100.0, 'CALL'): _q(0.5, atm_cum)}
        if itm_present:
            quotes[(99.0, 'CALL')] = _q(0.5, itm_cum)
        sig = det.check('AAPL', [_bar(i)], LEVELS, quotes, expiry=None, pc_ratio=None)
        if sig:
            last = sig
            all_sigs.append(sig[0])
    return all_sigs if collect else last


# Scenario A: ATM + ITM build a 5-bar cluster WITHOUT any single-bar extreme
# (per-bar delta 35 < single-print floor 300, so only the window qualifies)
# -> HIGH / ATM_ITM_CLUSTER
quiet = [10] * 10
ramp  = [35] * 5
sigA = run(quiet + ramp, quiet + ramp)
check("A: ATM+ITM cluster fires", len(sigA) == 1)
if sigA:
    s = sigA[0]
    check("A: confidence HIGH", s['confidence'] == 'HIGH')
    check("A: shape ATM_ITM_CLUSTER", s['signal_shape'] == 'ATM_ITM_CLUSTER')
    check("A: cluster_active_bars >= 3", s['cluster_active_bars'] >= 3)

# Scenario B: single fat ATM bar, ITM quiet -> EXTREME_SINGLE_PRINT / MEDIUM_HIGH
sigB = run(quiet + [500], quiet + [10])
check("B: single print fires", len(sigB) == 1)
if sigB:
    s = sigB[0]
    check("B: shape EXTREME_SINGLE_PRINT (not a cluster)",
          s['signal_shape'] == 'EXTREME_SINGLE_PRINT')
    check("B: confidence MEDIUM_HIGH (rank 2)", s['confidence'] == 'MEDIUM_HIGH')

# Scenario C: single print fires first, then a cluster forms on BOTH sides and
# UPGRADES to HIGH/ATM_ITM_CLUSTER (fresh alert, upgrade=True). A later HIGH does
# not re-fire.
d_atm = [10] * 10 + [500] + [180] * 6     # ATM: one extreme print, then sustained
d_itm = [10] * 11 + [180] * 6             # ITM: quiet, then joins the pressure
sigs = run(d_atm, d_itm, collect=True)
check("C: at least two alerts (single + upgrade)", len(sigs) >= 2)
if sigs:
    check("C: first alert is EXTREME_SINGLE_PRINT",
          sigs[0]['signal_shape'] == 'EXTREME_SINGLE_PRINT' and sigs[0]['upgrade'] is False)
highs = [s for s in sigs if s['confidence'] == 'HIGH']
check("C: exactly one HIGH upgrade fires (no re-fire)", len(highs) == 1)
if highs:
    check("C: upgrade flagged + ATM_ITM_CLUSTER",
          highs[0]['upgrade'] is True and highs[0]['signal_shape'] == 'ATM_ITM_CLUSTER')

# Scenario D: with upgrades disabled, the HIGH cluster must NOT supersede.
config.CLUSTER_UPGRADE_ENABLED = False
try:
    sigs_d = run(d_atm, d_itm, collect=True)
    check("D: no HIGH when upgrades disabled",
          all(s['confidence'] != 'HIGH' for s in sigs_d))
finally:
    config.CLUSTER_UPGRADE_ENABLED = True

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
raise SystemExit(1 if _fail else 0)

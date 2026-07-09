"""
§21 TESTS B/C/D — countertrend reversal-conviction gate.

  B  AMZN early 242.5P (575 / 1,017 / 5.4×) vs a strong, still-working bull trend
     → WATCH (no alert), COUNTERTREND_VOLUME_INSUFFICIENT, watch created.
  C  AMZN late 242.5P (~2,170, put leadership up, call flow fading) → REVERSAL (fires).
  D  NVDA 210P with no opposing established trend → CONTINUATION (ordinary path, not gated).

Run:  python test_countertrend.py   (exit 0 = pass)
"""
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis.signal_detector import SignalDetector
from data.market_utils import CST

T0 = datetime(2026, 6, 18, 9, 0, tzinfo=CST)


def _bull_trend(faded: bool):
    """Established AMZN bull trend (calls led the +1.25% move); optionally faded + late."""
    d = SignalDetector(); d._history_date = date(2026, 6, 18)
    d._trend.update('AMZN', 240.0, T0, {'call_leadership': 0.0, 'put_leadership': 0.0})        # open
    d._trend.update('AMZN', 243.0, T0 + timedelta(minutes=2),
                    {'call_leadership': 0.85, 'put_leadership': 0.10})                          # +1.25%, calls lead
    if faded:                                   # 17 min in: spot still +1%, calls faded, puts lead
        bt = T0 + timedelta(minutes=17)
        d._trend.update('AMZN', 242.5, bt, {'call_leadership': 0.30, 'put_leadership': 0.85})
        return d, bt, {'call_leadership': 0.30, 'put_leadership': 0.85}
    bt = T0 + timedelta(minutes=3)
    d._trend.update('AMZN', 243.0, bt, {'call_leadership': 0.85, 'put_leadership': 0.30})
    return d, bt, {'call_leadership': 0.85, 'put_leadership': 0.30}


def run() -> int:
    fails = 0

    # TEST B — early weak put vs working bull trend
    d, bt, ld = _bull_trend(faded=False)
    atm = {'peak_1m': 575, 'vol_3m': 1017, 'vol_5m': 1200, 'valid': False}
    dec, reason = d._countertrend_gate('AMZN', 'BEARISH', atm, {'valid': False}, ld, bt)
    sig = {'atm_vol_1m': 575, 'atm_spike_ratio': 5.4, 'premium_notional': 86000}
    if dec == 'WATCH' and reason == 'COUNTERTREND_VOLUME_INSUFFICIENT':
        d._note_countertrend_watch('AMZN', 'BEARISH', 'R3', sig, bt)
        watched = ('AMZN', 'BEARISH') in d._countertrend_watch
        print(f"[{'PASS' if watched else 'FAIL'}] TEST B: early put → {dec}/{reason}, watch_created={watched}")
        fails += 0 if watched else 1
    else:
        fails += 1; print(f"[FAIL] TEST B: expected WATCH/VOLUME_INSUFFICIENT, got {dec}/{reason}")

    # TEST C — late strong put, call flow faded
    d, bt, ld = _bull_trend(faded=True)
    atm = {'peak_1m': 2170, 'vol_3m': 2500, 'vol_5m': 3000, 'valid': True}
    dec, reason = d._countertrend_gate('AMZN', 'BEARISH', atm, {'valid': True}, ld, bt)
    if dec == 'REVERSAL':
        print(f"[PASS] TEST C: late conviction put → {dec} (fires as countertrend reversal)")
    else:
        fails += 1; print(f"[FAIL] TEST C: expected REVERSAL, got {dec}/{reason}")

    # TEST D — NVDA put, no opposing established trend
    d = SignalDetector(); d._history_date = date(2026, 6, 18)
    d._trend.update('NVDA', 210.0, T0, {'call_leadership': 0.0, 'put_leadership': 0.0})
    atm = {'peak_1m': 508, 'vol_3m': 520, 'vol_5m': 548, 'valid': True}
    dec, reason = d._countertrend_gate('NVDA', 'BEARISH', atm, {'valid': False},
                                       {'call_leadership': 0.2, 'put_leadership': 0.3},
                                       T0 + timedelta(minutes=5))
    if dec == 'CONTINUATION':
        print(f"[PASS] TEST D: NVDA put, no trend → {dec} (ordinary path, not countertrend-gated)")
    else:
        fails += 1; print(f"[FAIL] TEST D: expected CONTINUATION, got {dec}/{reason}")

    print(f"\n{3 - fails}/3 countertrend tests passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

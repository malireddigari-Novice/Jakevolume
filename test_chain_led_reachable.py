"""
Regression for the line-654 early-return bug: the chain-led / Route-B emergent path
must be REACHED by check() even when the primary-level path produces no candidate
(spot not near any level). Before the fix, `if not candidates: return []` ran before
the chain-led block, so non-level ATM flow (e.g. TSLA 395P @ 09:05 on 2026-07-09) was
never evaluated.

  TEST 1: no level near spot  -> _chain_led_entry IS still invoked (both sides).
  TEST 2: a level IS near spot -> _chain_led_entry still invoked (unchanged behavior).

Run:  python test_chain_led_reachable.py   (exit 0 = pass)
"""
import sys
from datetime import date, datetime

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis.signal_detector import SignalDetector
from data.market_utils import CST

_TODAY = date(2026, 7, 9)
_BT = datetime(2026, 7, 9, 9, 5, tzinfo=CST)


def _bars(close):
    return [{'close': close, 'bar_time': _BT} for _ in range(5)]


def _quotes(spot):
    """A minimal put+call chain around spot so check() has option data to process."""
    q = {}
    for s in (spot - 5, spot - 2.5, spot, spot + 2.5, spot + 5):
        for ot in ('CALL', 'PUT'):
            q[(float(s), ot)] = {'mark': 4.0, 'bid': 3.95, 'ask': 4.05,
                                 'day_low': 3.5, 'volume': 500}
    return q


def _detector_with_spy():
    d = SignalDetector()
    d._history_date = _TODAY          # skip the new-day reset mid-call
    calls = {'sides': []}
    real = d._chain_led_entry

    def spy(symbol, confirm_type, *a, **k):
        calls['sides'].append(confirm_type)
        return None, None             # don't need a real signal; only reachability
    d._chain_led_entry = spy
    return d, calls, real


def run() -> int:
    fails = 0
    if not config.CHAIN_LED_ENTRY_ENABLED:
        print("[SKIP] CHAIN_LED_ENTRY_ENABLED is off — test not meaningful")
        return 0

    spot = 397.88

    # TEST 1 — NO level anywhere near spot (all >1% away). Level path yields no
    # candidate; chain-led must still be reached.
    d, calls, _ = _detector_with_spy()
    far_levels = [{'level_type': 'SUPPORT', 'rank': 1, 'strike': 370.0},
                  {'level_type': 'SUPPORT', 'rank': 2, 'strike': 375.0},
                  {'level_type': 'RESISTANCE', 'rank': 1, 'strike': 420.0},
                  {'level_type': 'RESISTANCE', 'rank': 2, 'strike': 425.0}]
    out = d.check('TSLA', _bars(spot), far_levels, _quotes(spot), expiry=_TODAY)
    if set(calls['sides']) == {'CALL', 'PUT'}:
        print(f"[PASS] no-level: chain-led reached for both sides {calls['sides']}")
    else:
        fails += 1
        print(f"[FAIL] no-level: chain-led NOT reached (early return bug). sides={calls['sides']}")

    # TEST 2 — a level IS near spot: chain-led still runs (unchanged for the side
    # not already fired). Guards against a fix that only works when candidates empty.
    d, calls, _ = _detector_with_spy()
    near_levels = [{'level_type': 'RESISTANCE', 'rank': 1, 'strike': 398.0},
                   {'level_type': 'SUPPORT', 'rank': 1, 'strike': 370.0}]
    d.check('TSLA', _bars(spot), near_levels, _quotes(spot), expiry=_TODAY)
    if calls['sides']:
        print(f"[PASS] near-level: chain-led still reached. sides={calls['sides']}")
    else:
        fails += 1
        print(f"[FAIL] near-level: chain-led not reached. sides={calls['sides']}")

    print(f"\n{2 - fails}/2 chain-led reachability tests passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

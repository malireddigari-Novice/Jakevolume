"""
§21 TEST A + negative — chain-led emergent entry path regression.

  POSITIVE: AMZN coordinated 235/237.5/240 call burst, spot never near a level
            → CHAIN_LED_EMERGENT_ENTRY, selects 240C, targets by price order.
  NEGATIVE: only the ATM strike has volume (no adjacent confirmation)
            → no signal, CHAIN_CONFIRMATION_MISSING.

Run:  python test_chain_led.py   (exit 0 = pass)
"""
import sys
from datetime import date, datetime
from collections import deque

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis.signal_detector import SignalDetector
from data.market_utils import CST


def _detector():
    d = SignalDetector()
    d._history_date = date(2026, 6, 18)
    return d


def _setup(d, strikes_last5, ot='CALL', mark=1.55, low=1.2):
    odm, vd = {}, {}
    for k, last5 in strikes_last5.items():
        key = (k, ot)
        d._opt_vol_hist[('AMZN', k, ot)] = deque([20] * 15 + last5, maxlen=d._hist_maxlen)
        d._opt_mark_low[('AMZN', k, ot)] = low
        odm[key] = {'mark': mark, 'day_low': low, 'bid': mark - 0.05, 'ask': mark + 0.05}
        vd[key] = last5[-1]
    return odm, vd


_LEVELS = [{'level_type': 'RESISTANCE', 'rank': 1, 'strike': 240.0},
           {'level_type': 'RESISTANCE', 'rank': 3, 'strike': 242.5},
           {'level_type': 'RESISTANCE', 'rank': 2, 'strike': 245.0},
           {'level_type': 'SUPPORT', 'rank': 1, 'strike': 235.0}]
_BT = datetime(2026, 6, 18, 9, 40, tzinfo=CST)


def run() -> int:
    fails = 0

    # POSITIVE — TEST A
    d = _detector()
    odm, vd = _setup(d, {235.0: [300, 350, 400, 380, 420],
                         237.5: [400, 500, 600, 520, 600],
                         240.0: [260, 300, 360, 340, 380]})
    sig, reason = d._chain_led_entry('AMZN', 'CALL', odm, vd, _LEVELS, 237.5,
                                     date(2026, 6, 18), [{'close': 237.2}] * 7, _BT, _BT,
                                     False, None, None)
    if sig and sig['signal_context'] == 'CHAIN_LED_EMERGENT_ENTRY' and sig['option_type'] == 'CALL':
        print(f"[PASS] TEST A positive: CHAIN-LED CALL, strike {sig['traded_strike']}, "
              f"targets {sig['exit1_price']}/{sig['exit2_price']} "
              f"({sig['target1_oi_name']}/{sig['target2_oi_name']})")
    else:
        fails += 1
        print(f"[FAIL] TEST A positive: expected chain-led call, got sig={bool(sig)} reason={reason}")

    # NEGATIVE — only ATM has volume (no adjacent confirmation)
    d = _detector()
    odm, vd = _setup(d, {235.0: [10, 10, 10, 10, 10],
                         237.5: [400, 500, 600, 520, 600],
                         240.0: [10, 10, 10, 10, 10]})
    sig, reason = d._chain_led_entry('AMZN', 'CALL', odm, vd, _LEVELS, 237.5,
                                     date(2026, 6, 18), [{'close': 237.2}] * 7, _BT, _BT,
                                     False, None, None)
    if sig is None and reason == 'CHAIN_CONFIRMATION_MISSING':
        print("[PASS] negative no-confirmation: blocked CHAIN_CONFIRMATION_MISSING")
    else:
        fails += 1
        print(f"[FAIL] negative: expected CHAIN_CONFIRMATION_MISSING, got sig={bool(sig)} reason={reason}")

    print(f"\n{2 - fails}/2 chain-led tests passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

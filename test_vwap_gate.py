"""Focused check: the §16 VWAP trend gate blocks against-trend, allows with-trend."""
import logging
from datetime import datetime

import config
from data.market_utils import CST
from analysis.signal_detector import SignalDetector


class _Capture(logging.Handler):
    def __init__(self): super().__init__(); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage())


def _run(session_vwap):
    cap = _Capture()
    log = logging.getLogger('analysis.signal_detector')
    log.setLevel(logging.INFO); log.addHandler(cap)
    try:
        det = SignalDetector()
        bt = datetime(2026, 6, 11, 9, 30, tzinfo=CST)
        bars = [{'bar_time': bt, 'open': 100.0, 'high': 100.0, 'low': 100.0,
                 'close': 100.0, 'volume': 1000}]
        levels = [{'level_type': 'SUPPORT', 'rank': 1, 'strike': 100.0}]   # BULLISH, at spot
        quotes = {(100.0, 'CALL'): {'volume': 0, 'mark': 1.0, 'bid': 0.9, 'ask': 1.1,
                                    'open_interest': 100}}
        sigs = det.check('AAPL', bars, levels, quotes, session_vwap=session_vwap)
        return sigs, cap.msgs
    finally:
        log.removeHandler(cap)


assert config.VWAP_GATE_ENABLED, "gate should default ON"

# spot=100, BULLISH. VWAP=105 → spot below VWAP → AGAINST trend → blocked.
sigs, msgs = _run(session_vwap=105.0)
assert not sigs, "against-trend BULLISH should not fire"
assert any('AGAINST_VWAP_TREND' in m for m in msgs), f"expected gate reason, got: {msgs}"
print("PASS  against-trend (spot 100 < vwap 105): blocked AGAINST_VWAP_TREND")

# spot=100, BULLISH. VWAP=95 → spot above VWAP → WITH trend → gate passes
# (then blocked downstream on volume, NOT by the VWAP gate).
sigs, msgs = _run(session_vwap=95.0)
assert not any('AGAINST_VWAP_TREND' in m for m in msgs), \
    f"with-trend must not be VWAP-blocked, got: {msgs}"
assert any('NO_VALID_VOLUME_SIGNAL' in m or 'CONTRACT_CHASED' in m or 'NO_QUOTES' in m
           for m in msgs), f"expected to reach volume eval, got: {msgs}"
print("PASS  with-trend (spot 100 > vwap 95): passed VWAP gate, blocked later on volume")

# Disabled / no VWAP → no-op (not blocked by the gate).
sigs, msgs = _run(session_vwap=None)
assert not any('AGAINST_VWAP_TREND' in m for m in msgs), "None VWAP must be a no-op"
print("PASS  session_vwap=None: gate is a no-op")

print("\nAll VWAP-gate sanity checks passed.")

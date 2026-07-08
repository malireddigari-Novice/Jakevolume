"""
Rolling per-contract volume tracker (P-ET step 1).

The seam for event-time / rolling-window volume. The DEFAULT backend maps to the
existing 1-min bar deltas the detector already computes, so it is numerically
identical to today's peak_1m / vol_3m:

    r60   (rolling 60s)  -> the latest 1-min delta
    r180  (rolling 180s) -> sum of the last 3 one-min deltas
    peak_1m              -> the current 1-min delta (matches _eval_volume)

Honest granularity note: at the current 60s poll cadence with cumulative-day-volume
snapshots there is no true sub-minute data, so r60/r180 are 1-min mappings. A future
TradeStreamBackend (Databento OPRA trades) can subclass this to provide real seconds
without changing callers or the production floor.
"""
from collections import deque


class RollingVolume:
    """Bar-delta backed rolling volume. One instance per (symbol, strike, option_type)."""

    def __init__(self, maxlen: int = 5) -> None:
        # keep enough deltas for the 3-bar (r180) / 5-bar windows
        self._deltas: deque = deque(maxlen=max(3, maxlen))

    def observe_delta(self, delta) -> None:
        """Ingest one completed/partial 1-min volume delta (oldest→newest)."""
        self._deltas.append(int(delta or 0))

    def r60(self) -> int:
        """Rolling 60s ≈ the latest 1-min delta."""
        return self._deltas[-1] if self._deltas else 0

    def r180(self) -> int:
        """Rolling 180s ≈ sum of the last 3 one-min deltas."""
        return sum(list(self._deltas)[-3:])

    def peak_1m(self) -> int:
        """Current 1-min volume (matches _eval_volume's peak1m = current bar)."""
        return self._deltas[-1] if self._deltas else 0

    def volume_pass(self, floor_60s: int, floor_180s: int) -> bool:
        """Production floor: at least one absolute rolling floor cleared (ratio can't override)."""
        return self.r60() >= floor_60s or self.r180() >= floor_180s

    def reset(self) -> None:
        self._deltas.clear()

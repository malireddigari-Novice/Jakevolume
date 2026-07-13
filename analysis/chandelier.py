"""
Chandelier trailing exit on the UNDERLYING (trail the runner).

Classic chandelier stop, anchored to the trade's best excursion since entry so it
ratchets and never loosens:

    BULLISH (long call):  stop = highest_high_since_entry - ATR(period) * mult
    BEARISH (long put):   stop = lowest_low_since_entry  + ATR(period) * mult

The runner (the half left after Exit1) stops out when the underlying reverses through
the stop. ATR is the volatility scale (wider stop in fast tape, tighter when calm).
Pure functions over 1-min OHLC bars (oldest -> newest); no state persisted — the
since-entry extreme is recomputed from bars each poll, so the trail is monotonic by
construction (the extreme only moves favorably).
"""
from typing import Optional


def true_range(high: float, low: float, prev_close: float) -> float:
    """Wilder true range for one bar."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(bars: list, period: int) -> Optional[float]:
    """Average true range over the last `period` bars (simple mean of TRs). None if
    there aren't enough bars (need period+1 to form `period` true ranges)."""
    if not bars or len(bars) < period + 1:
        return None
    trs = [true_range(bars[i]['high'], bars[i]['low'], bars[i - 1]['close'])
           for i in range(1, len(bars))]
    recent = trs[-period:]
    if len(recent) < period:
        return None
    return sum(recent) / len(recent)


def running_extreme(bars: list, side: str) -> Optional[float]:
    """Best excursion since entry: highest high (BULLISH) / lowest low (BEARISH)."""
    if not bars:
        return None
    if side == 'BULLISH':
        return max(b['high'] for b in bars)
    return min(b['low'] for b in bars)


def chandelier_stop(side: str, extreme: Optional[float], atr_val: Optional[float],
                    mult: float) -> Optional[float]:
    """Trailing stop level on the underlying, or None if inputs are missing."""
    if extreme is None or atr_val is None:
        return None
    return round(extreme - atr_val * mult, 4) if side == 'BULLISH' \
        else round(extreme + atr_val * mult, 4)


def evaluate(side: str, spot: float, bars_since_entry: list, *,
             period: int, mult: float) -> dict:
    """
    Chandelier verdict for the current poll:
      {ready, stop, atr, extreme, exit}
    `ready` is False until there are enough bars for ATR (don't stop out blind).
    `exit` is True once the underlying has reversed through the trailing stop.
    """
    a   = atr(bars_since_entry, period)
    ext = running_extreme(bars_since_entry, side)
    stop = chandelier_stop(side, ext, a, mult)
    if stop is None or spot is None:
        return {'ready': False, 'stop': stop, 'atr': a, 'extreme': ext, 'exit': False}
    breached = (spot <= stop) if side == 'BULLISH' else (spot >= stop)
    return {'ready': True, 'stop': stop, 'atr': round(a, 4), 'extreme': ext,
            'exit': bool(breached)}

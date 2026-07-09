"""
Countertrend classification + strict Gold gate (P4, §11-§12).

A signal that opposes an established intraday move needs stricter evidence. A call at
support during a dominant bearish trend is NOT a normal bounce — it is a countertrend
watch unless a confirmed reversal passes (addresses TSLA 412.5C after a bearish open).
"""
import config


def established_move_pct(spot: float, session_open: float) -> float:
    return abs(spot - session_open) / session_open if session_open else 0.0


def is_established_trend(spot: float, session_open: float) -> bool:
    """True once the underlying has moved >= ESTABLISHED_MOVE_PCT from the session open."""
    return established_move_pct(spot, session_open) >= config.ESTABLISHED_MOVE_PCT


def countertrend_floors(symbol: str):
    """Stricter absolute floors for a countertrend event (1m, 3m)."""
    if symbol in config.VOLATILE_SYMBOLS:          # NVDA / TSLA
        return 1500, 3000
    return 1250, 2500                              # standard MAG-7


def countertrend_label(*, symbol: str, peak_1m: int, vol_3m: int,
                        multi_or_exceptional: bool, prior_trend_faded: bool,
                        fresh_prior_conviction: bool, structure_reclaimed: bool) -> str:
    """
    GOLD_CONFIRMED_COUNTERTREND_REVERSAL only when ALL strict conditions pass; otherwise
    COUNTERTREND_WATCH (no production trade).
    """
    f1, f3 = countertrend_floors(symbol)
    vol_ok = (peak_1m or 0) >= f1 or (vol_3m or 0) >= f3
    confirmed = (vol_ok and multi_or_exceptional and prior_trend_faded
                 and not fresh_prior_conviction and structure_reclaimed)
    return 'GOLD_CONFIRMED_COUNTERTREND_REVERSAL' if confirmed else 'COUNTERTREND_WATCH'

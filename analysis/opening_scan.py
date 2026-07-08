"""
Opening ATM±N event-time scan (P-ET step 5).

During the opening window, surface contracts that crossed the production floor at
EVENT time and were within ATM ± OPENING_STRIKE_WINDOW strikes AT THAT MOMENT — using
the frozen event-time distance, so a contract that flowed at the open and then ran
ITM/OTM before the bar closed stays eligible (the TSLA-425P failure). Unlike the
level path, this looks across the nearby chain, not only S/R-level contracts.

Research-only for now: scan_opening() returns eligible candidates for logging/audit; it
does NOT auto-fire. Promotion to production goes through the Gold gate after the P6
control tests (AAPL 310C breakout, TSLA 425P opening) validate it.
"""
from typing import Optional


def strike_increment(option_quotes: dict, default: float = 2.5) -> float:
    """Smallest positive gap between adjacent strikes in the chain (fallback `default`)."""
    ks = sorted({float(s) for (s, _ot) in option_quotes})
    diffs = [round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a]
    return min(diffs) if diffs else default


def event_time_eligible(event_state, window_strikes: int, increment: float) -> bool:
    """
    True iff the contract crossed the production floor AND was within ATM ± window
    strikes AT EVENT TIME (frozen), regardless of where spot/ATM are now.
    """
    if event_state is None or not event_state.crossed:
        return False
    d = event_state.strike_distance_strikes(increment)
    return d is not None and d <= window_strikes


def scan_opening(symbol: str, option_quotes: dict, event_reg,
                 *, window_strikes: int, increment: Optional[float] = None) -> list:
    """
    Return event-time-eligible, floor-crossed contracts across the nearby chain for the
    opening window. Each item: {symbol, strike, option_type, event_state, no_retro,
    dist_strikes}. Purely reads the registry — no firing decision here.
    """
    incr = increment if increment else strike_increment(option_quotes)
    out = []
    for (strike, otype) in option_quotes:
        es = event_reg.get(symbol, float(strike), otype)
        if event_time_eligible(es, window_strikes, incr):
            out.append({
                'symbol': symbol, 'strike': float(strike), 'option_type': otype,
                'event_state': es, 'no_retro': es.no_retro_label(),
                'dist_strikes': es.strike_distance_strikes(incr),
            })
    return out

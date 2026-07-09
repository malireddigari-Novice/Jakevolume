"""
Realistic, timestamp-synchronized paper fill (P5, §1/§13).

The alert price must be an EXECUTABLE estimate at the actual commit moment (near the
ask), not a stale mark — and when the commit fill lands far from where the event
printed (the NVDA 195C $1.20-vs-a-$0.89-1.03 bar), that must be flagged, not hidden.

  executable_fill(bid, ask, mark) -> (price, method)
      long-option entry near the ask; midpoint fallback; mark only as a last resort.
  price_moved_from_event(fill, event_ref, tol) -> bool
      True when the commit fill deviates from the event-time reference by > tol, i.e.
      price ran away between the flow and the fill (needs explanation / de-prioritize).
"""


def executable_fill(bid, ask, mark):
    """Executable long-entry estimate + method label. You pay the ask to buy, so the
    ask at commit is the realistic fill; mark then bid are fallbacks when no ask."""
    if ask and float(ask) > 0:
        return round(float(ask), 2), 'ASK_AT_COMMIT'
    if mark and float(mark) > 0:
        return round(float(mark), 2), 'MARK_FALLBACK'
    if bid and float(bid) > 0:
        return round(float(bid), 2), 'BID_FALLBACK'
    return None, 'NONE'


def price_moved_from_event(fill, event_ref, tol: float = 0.15) -> bool:
    """True if the commit fill is > tol away from the event-time reference price."""
    if fill is None or event_ref is None or float(event_ref) <= 0:
        return False
    return abs(float(fill) - float(event_ref)) / float(event_ref) > tol

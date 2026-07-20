"""
Economically-weighted directional flow (#5).

Raw call-vs-put contract count is a poor leadership signal: 5,000 contracts at $0.05
is not 1,000 contracts at $1.00, and far-OTM dead inventory or penny churn shouldn't
move the needle. Leadership should answer "which side is committing FRESH, RELEVANT
premium right now?" — so each contract's contribution is weighted by dollars, not count:

    FreshDirectionalWeight = FreshEventVolume × Mark × 100         (premium notional)
                             × RelevanceToSpot                     (ATM matters, far-OTM doesn't)
                             × ConcentrationWeight                 (a burst, not background)
                             × PremiumDiscoveryWeight               (fresh > recycled)

Pure functions; no I/O. Used by the VOLUME_LEADER route (#2) and, when
ECONOMIC_LEADERSHIP_ENABLED, as the leadership input.
"""
from typing import Optional

import config

# PDS class → discovery weight (fresh flow counts fully, recycled/accepted less).
_PDS_WEIGHT = {
    'VIRGIN_DISCOVERY': 1.00, 'FRESH_ACCUMULATION': 1.00,
    'ACCEPTED_VALUE': 0.50, 'REPRICED_RECYCLED': 0.25, 'EXHAUSTED': 0.10,
}


def relevance_to_spot(spot: Optional[float], strike: float, band_pct: float = 0.03) -> float:
    """1.0 at ATM, decaying linearly to 0 at `band_pct` (default 3%) from spot."""
    if not spot or spot <= 0:
        return 1.0
    d = abs(strike - spot) / spot
    return max(0.0, 1.0 - d / band_pct)


def concentration_weight(event_share: Optional[float]) -> float:
    """A concentrated burst (high 5m/20m share) counts more than distributed background."""
    if event_share is None:
        return 0.5
    return min(1.0, max(0.0, float(event_share)))


def discovery_weight(pds_class: Optional[str]) -> float:
    """Fresh premium discovery counts fully; recycled/exhausted premium is discounted."""
    if pds_class is None:
        return 1.0                      # unknown → neutral (don't penalise absent PDS)
    return _PDS_WEIGHT.get(pds_class, 1.0)


def directional_weight(*, event_vol, mark, spot, strike, event_share, pds_class=None) -> float:
    """Dollar-weighted fresh directional contribution of one contract."""
    if not event_vol or not mark or mark <= 0:
        return 0.0
    notional = float(event_vol) * float(mark) * 100.0
    return (notional
            * relevance_to_spot(spot, strike)
            * concentration_weight(event_share)
            * discovery_weight(pds_class))


def weighted_leadership(contracts: list, spot: Optional[float]) -> dict:
    """
    Aggregate dollar-weighted call vs put pressure across active contracts.

    contracts: list of {strike, side ('CALL'/'PUT'), event_vol, mark, event_share, pds_class}.
    Returns call/put weights, the dominant side, its share of the total, and the
    dollar ratio — so a caller can ask "does the opposite side match or exceed us?".
    """
    call_w = put_w = 0.0
    for c in contracts:
        w = directional_weight(event_vol=c.get('event_vol'), mark=c.get('mark'),
                               spot=spot, strike=c.get('strike'),
                               event_share=c.get('event_share'), pds_class=c.get('pds_class'))
        if c.get('side') == 'CALL':
            call_w += w
        elif c.get('side') == 'PUT':
            put_w += w
    total = call_w + put_w
    dominant = 'CALL' if call_w >= put_w else 'PUT'
    lead, opp = (call_w, put_w) if dominant == 'CALL' else (put_w, call_w)
    return {
        'call_weight': round(call_w), 'put_weight': round(put_w),
        'dominant': dominant if total > 0 else None,
        'dominant_share': round(lead / total, 3) if total > 0 else 0.0,
        'ratio': round(lead / max(opp, 1.0), 2),
        'total': round(total),
    }

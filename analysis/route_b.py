"""
Chain-led Route B — exceptional single-strike conviction (P3).

Normally a chain-led entry needs an ATM/near-ATM strike PLUS an adjacent strike (Route A).
Route B lets one exceptionally strong strike qualify WITHOUT adjacent confirmation, for
events like MSFT 380C / TSLA 425C where a single strike printed ~2K+ in one minute.

Requires ALL of:
  - single-strike rolling-60s (peak_1m) >= GOLD_EXCEPTIONAL_SINGLE_1M (default 2000)
  - within 2 strikes of the (event-time) ATM
  - meaningful premium notional (>= GOLD_MIN_PREMIUM_NOTIONAL; 0 default = not enforced yet)
  - acceptable contract value (not chased/elevated)
  - concentrated (not distributed background flow)
  - opposite side does not dominate
Directional-intent / activation is validated separately by the intent gate.
"""
import config

_OK_VALUE_REGIONS = {'GOLD_VALUE_LOCATION', 'STRONG_VALUE_LOCATION',
                     'ACCEPTABLE_ONLY_WITH_EXCEPTIONAL_EVIDENCE'}


def route_b_qualifies(*, peak_1m: int, strikes_from_atm: int, premium_notional: float,
                      clow_region, concentrated: bool, opposite_dominates: bool) -> bool:
    """True when a single strike is exceptional enough to stand in for adjacent confirmation."""
    if (peak_1m or 0) < config.GOLD_EXCEPTIONAL_SINGLE_1M:
        return False
    if strikes_from_atm is None or strikes_from_atm > 2:
        return False
    if (premium_notional or 0) < config.GOLD_MIN_PREMIUM_NOTIONAL:
        return False
    if clow_region is not None and clow_region not in _OK_VALUE_REGIONS:
        return False
    if not concentrated:
        return False
    if opposite_dominates:
        return False
    return True

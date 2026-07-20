"""
VOLUME_LEADER standalone entry route (#2).

Not every real directional event immediately produces coordinated volume across three
strikes. GOOGL 370P was exceptionally clean at ONE strike; the chain-led path can
reject that class because it demands ATM + adjacent + combined chain floors +
multi-strike concentration. This route lets a single currently-relevant strike qualify
on its OWN institutional-scale evidence — adjacent strikes strengthen it but aren't
mandatory.

Pure qualification logic (no I/O). The detector calls qualifies() per side against the
dominant near-ATM strike; a pass builds a signal via the normal machinery.

Qualifies when ALL hold:
  1. Contract is ATM / 1-ITM / 1-OTM (moneyness ≤ VOLUME_LEADER_MONEYNESS strikes from ATM).
  2. Completed 1-min volume is exceptional (≥ per-symbol floor) OR premium notional is.
  3. Premium notional ≥ per-symbol floor.
  4. Contract within the premium-low region (low_dist ≤ VOLUME_LEADER_LOW_DIST_MAX).
  5. Concentrated event, not persistent background (event_share ≥ min, not persistent).
  6. Fresh vs prior same-contract participation (PDS not recycled/accepted/exhausted; unknown OK).
  7. Opposite-side economically-weighted flow does not match or exceed this side.
  8. Contract currently relevant to spot (covered by moneyness in 1).
"""
from typing import Optional

import config


def _sym(mapping: dict, symbol: str):
    return mapping.get(symbol, mapping['default'])


def qualifies(symbol: str, side: str, *, moneyness_strikes: int, completed_vol: Optional[int],
              premium_notional: Optional[float], low_dist: Optional[float],
              event_share: Optional[float], persistent_bg: bool,
              pds_class: Optional[str], same_weight: float, opp_weight: float) -> dict:
    """
    Return {'qualifies': bool, 'reasons': {gate: pass_bool}, 'block': first-failing-gate|None}.
    completed_vol is the CLOSED 1-min bar volume (never a partial), so this fires on
    proven institutional-scale participation.
    """
    vol_min      = _sym(config.VOLUME_LEADER_1M_MIN, symbol)
    notional_min = _sym(config.VOLUME_LEADER_NOTIONAL_MIN, symbol)
    cv = int(completed_vol or 0)
    pn = float(premium_notional or 0.0)

    g = {}
    g['moneyness']    = moneyness_strikes is not None and moneyness_strikes <= config.VOLUME_LEADER_MONEYNESS
    g['exceptional']  = cv >= vol_min or pn >= notional_min
    g['notional']     = pn >= notional_min
    g['premium_low']  = low_dist is None or low_dist <= config.VOLUME_LEADER_LOW_DIST_MAX
    g['concentrated'] = (event_share is None or event_share >= config.VOLUME_LEADER_EVENT_SHARE) and not persistent_bg
    g['fresh']        = pds_class in (None, 'VIRGIN_DISCOVERY', 'FRESH_ACCUMULATION')
    g['leads']        = same_weight > opp_weight
    order = ['moneyness', 'exceptional', 'notional', 'premium_low', 'concentrated', 'fresh', 'leads']
    block = next((k for k in order if not g[k]), None)
    return {'qualifies': block is None, 'reasons': g, 'block': block}

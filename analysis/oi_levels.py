"""
OI-based support / resistance level computation.

Algorithm
---------
1. Restrict the option chain to strikes within ATM_RANGE_PCT of the current price.
2. Resistance = call OI strikes near price  → watch PUT volume clustering there.
3. Support    = put  OI strikes near price  → watch CALL volume clustering there.
4. Deduplicate strikes so each appears at most once per side.
5. Rank by PROXIMITY to spot price: the strike closest to spot = rank 1 (S1/R1),
   the next closest = rank 2 (S2/R2).  High-OI strikes further from spot are less
   immediately relevant as intraday S/R.

Returns a flat list of level dicts ready for db.save_oi_levels().
"""
import logging

import config

logger = logging.getLogger(__name__)


def compute_oi_levels(
    chain: dict,
    underlying_price: float,
    atm_range_pct: float | None = None,
    top_n: int | None = None,
) -> list[dict]:
    """
    Compute support / resistance levels from a normalised option chain dict.

    Levels are ranked by proximity to spot price (closest = rank 1) so that
    S1/R1 is always the most immediately relevant intraday level.

    Parameters
    ----------
    chain : dict
        Output of DatabentoClient.get_option_chain().
    underlying_price : float
        Current mark price of the underlying (used as the ATM anchor).
    atm_range_pct : float, optional
        Half-width of the ATM strike band as a fraction of price.
        Defaults to config.ATM_RANGE_PCT.
    top_n : int, optional
        Number of levels per side. Defaults to config.TOP_N_LEVELS.

    Returns
    -------
    list[dict]  — each dict has keys:
        level_type   : 'SUPPORT' | 'RESISTANCE'
        rank         : 1 (closest to spot) … top_n (furthest)
        strike       : float
        open_interest: int
        option_type  : 'CALL' | 'PUT'
    """
    atm_range_pct = atm_range_pct or config.ATM_RANGE_PCT
    top_n         = top_n or config.TOP_N_LEVELS

    price = underlying_price
    lo    = price * (1 - atm_range_pct)
    hi    = price * (1 + atm_range_pct)

    calls_near = [
        c for c in chain['calls']
        if lo <= c['strike'] <= hi and c['open_interest'] > 0
    ]
    puts_near = [
        p for p in chain['puts']
        if lo <= p['strike'] <= hi and p['open_interest'] > 0
    ]

    # Rank by proximity to spot: closest strike with OI = rank 1
    calls_ranked = _top_by_proximity(calls_near, top_n, price)
    puts_ranked  = _top_by_proximity(puts_near,  top_n, price)

    levels: list[dict] = []

    for rank, c in enumerate(calls_ranked, start=1):
        levels.append({
            'level_type':    'RESISTANCE',
            'rank':          rank,
            'strike':        c['strike'],
            'open_interest': c['open_interest'],
            'option_type':   'PUT',   # watch PUT volume clustering at resistance
        })

    for rank, p in enumerate(puts_ranked, start=1):
        levels.append({
            'level_type':    'SUPPORT',
            'rank':          rank,
            'strike':        p['strike'],
            'open_interest': p['open_interest'],
            'option_type':   'CALL',  # watch CALL volume clustering at support
        })

    logger.info(
        "OI levels for price=%.4f: %d resistance, %d support (ATM±%.1f%%) "
        "[ranked by proximity]",
        price, len(calls_ranked), len(puts_ranked), atm_range_pct * 100,
    )
    return levels


def get_top_oi_snapshot(
    chain: dict,
    underlying_price: float,
    top_n: int = 2,
) -> dict:
    """
    Return the top N call and put contracts by OI near ATM.

    Used for the daily OI_Snapshot sheet — shows absolute highest OI strikes
    for reference, independent of S/R level ranking.
    """
    half = underlying_price * config.ATM_RANGE_PCT
    lo   = underlying_price - half
    hi   = underlying_price + half

    def top(contracts: list) -> list[dict]:
        nearby = [
            c for c in contracts
            if lo <= c['strike'] <= hi and c['open_interest'] > 0
        ]
        return _top_by_oi(nearby, top_n)

    return {
        'top_calls': top(chain['calls']),
        'top_puts':  top(chain['puts']),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _top_by_proximity(contracts: list[dict], n: int, spot: float) -> list[dict]:
    """
    Deduplicate by strike (keep highest OI), then rank by proximity to spot.

    Closest strike to spot = index 0 (rank 1).  Only strikes with OI > 0
    reach this function; the caller's list comprehension enforces that.
    """
    by_strike: dict[float, dict] = {}
    for c in contracts:
        s = c['strike']
        if s not in by_strike or c['open_interest'] > by_strike[s]['open_interest']:
            by_strike[s] = c
    ranked = sorted(by_strike.values(), key=lambda x: abs(x['strike'] - spot))
    return ranked[:n]


def _top_by_oi(contracts: list[dict], n: int) -> list[dict]:
    """Deduplicate by strike (keep highest OI), then return top-n by OI magnitude."""
    by_strike: dict[float, dict] = {}
    for c in contracts:
        s = c['strike']
        if s not in by_strike or c['open_interest'] > by_strike[s]['open_interest']:
            by_strike[s] = c
    ranked = sorted(by_strike.values(), key=lambda x: x['open_interest'], reverse=True)
    return ranked[:n]

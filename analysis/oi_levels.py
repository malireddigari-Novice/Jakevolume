"""
OI-based support / resistance level computation — Simplified V1 (8:20 AM CST).

Algorithm (§3 — 6 levels per symbol)
------------------------------------
Anchor on the pre-market spot (Spot0). Consider only strikes within ±5% of spot.

  CALL RESISTANCE : valid call strikes   Spot0 <= Strike <= Spot0 * 1.05
                    rank by CALL open interest, top 3 → R1, R2, R3
  PUT  SUPPORT    : valid put strikes     Spot0 * 0.95 <= Strike <= Spot0
                    rank by PUT  open interest, top 3 → S1, S2, S3

Rank 1 is the highest-OI strike on each side (R1/S1), not the nearest. These are
watch zones only; intraday proximity + volume decide whether anything fires.

Level semantics
---------------
  RESISTANCE  (R1/R2/R3)  — highest Call OI in [spot, spot*1.05]
  SUPPORT     (S1/S2/S3)  — highest Put  OI in [spot*0.95, spot]
"""
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)


def compute_oi_levels(
    chain: dict,
    underlying_price: float,
) -> list[dict]:
    """
    Compute up to 6 S/R levels (R1-R3, S1-S3) from a normalised option chain.

    Parameters
    ----------
    chain            : Output of DatabentoClient.get_option_chain() or equivalent.
    underlying_price : 8:20 AM spot price used as the band anchor (Spot0).

    Returns
    -------
    list[dict]  — each dict:
        level_type    : 'SUPPORT' | 'RESISTANCE'
        rank          : 1 | 2 | 3   (1 = highest OI on that side)
        strike        : float
        open_interest : int
        option_type   : 'CALL' (resistance) | 'PUT' (support)
        expiry        : date | None
    """
    all_contracts = chain.get('all', [])
    if not all_contracts:
        return []

    spot = underlying_price
    chain_expiry: Optional[object] = chain.get('expiry')
    band = config.OI_LEVEL_BAND_PCT
    call_hi = spot * (1 + band)
    put_lo  = spot * (1 - band)

    # Top-3 by OI within the ±band window, deduplicated by strike (keep highest
    # OI at each strike). Calls at/above spot → resistance; puts at/below → support.
    call_in_band = [c for c in all_contracts
                    if c['option_type'] == 'CALL' and spot <= float(c['strike']) <= call_hi]
    put_in_band  = [c for c in all_contracts
                    if c['option_type'] == 'PUT'  and put_lo <= float(c['strike']) <= spot]

    levels: list[dict] = []
    for rank, c in enumerate(_top_by_oi(call_in_band, config.TOP_N_LEVELS), start=1):
        levels.append(_make_level('RESISTANCE', rank, c, chain_expiry))
    for rank, c in enumerate(_top_by_oi(put_in_band, config.TOP_N_LEVELS), start=1):
        levels.append(_make_level('SUPPORT', rank, c, chain_expiry))

    # ── Logging ──────────────────────────────────────────────────────────────
    rl = _side(levels, 'RESISTANCE')
    sl = _side(levels, 'SUPPORT')

    def _fmt(side: list, i: int) -> str:
        if i < len(side):
            return f"{side[i]['strike']:.2f}(OI={side[i]['open_interest']:,})"
        return '-'

    logger.info(
        "OI levels  price=%.4f  band=±%.0f%% | "
        "R1=%s  R2=%s  R3=%s | S1=%s  S2=%s  S3=%s",
        spot, config.OI_LEVEL_BAND_PCT * 100,
        _fmt(rl, 0), _fmt(rl, 1), _fmt(rl, 2),
        _fmt(sl, 0), _fmt(sl, 1), _fmt(sl, 2),
    )
    return levels


def get_top_oi_snapshot(
    chain: dict,
    underlying_price: float,
    top_n: int = 2,
) -> dict:
    """
    Return the top N call and put contracts by raw OI near ATM.

    Used for the OI_Snapshot sheet — shows absolute highest-OI strikes
    for reference, independent of S/R level ranking.
    """
    half = underlying_price * config.ATM_RANGE_PCT
    lo   = underlying_price - half
    hi   = underlying_price + half

    def top(contracts: list) -> list[dict]:
        nearby = [c for c in contracts if lo <= c['strike'] <= hi and c.get('open_interest', 0) > 0]
        return _top_by_oi(nearby, top_n)

    return {
        'top_calls': top(chain['calls']),
        'top_puts':  top(chain['puts']),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _top_by_oi(contracts: list[dict], n: int) -> list[dict]:
    """Deduplicate by strike (keep highest OI), return top-n sorted by OI descending."""
    by_strike: dict[float, dict] = {}
    for c in contracts:
        s = float(c['strike'])
        if s not in by_strike or c.get('open_interest', 0) > by_strike[s].get('open_interest', 0):
            by_strike[s] = c
    return sorted(by_strike.values(), key=lambda x: x.get('open_interest', 0), reverse=True)[:n]


def _make_level(
    level_type: str,
    rank: int,
    contract: dict,
    chain_expiry,
) -> dict:
    return {
        'level_type':    level_type,
        'rank':          rank,
        'strike':        float(contract['strike']),
        'open_interest': int(contract.get('open_interest', 0)),
        'option_type':   'CALL' if level_type == 'RESISTANCE' else 'PUT',
        'expiry':        contract.get('expiry', chain_expiry),
    }


def _side(levels: list[dict], level_type: str) -> list[dict]:
    return sorted(
        [lv for lv in levels if lv['level_type'] == level_type],
        key=lambda x: x['rank'],
    )

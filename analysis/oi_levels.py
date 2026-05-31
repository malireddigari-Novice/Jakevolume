"""
OI-based support / resistance level computation — Step-1 spec (8:20 AM CST).

Algorithm (6 levels per symbol)
---------------------------------
R1 = nearest call strike strictly above ATM (proximity).
S1 = nearest put  strike at or below ATM (proximity — ATM strike included).

R2 = highest OI among the NEXT 2 call strikes above ATM after R1.
S2 = highest OI among the NEXT 2 put  strikes below ATM after S1.

R3 = highest OI among the next pair of 2 call strikes after the R2 window.
S3 = highest OI among the next pair of 2 put  strikes after the S2 window.

Example — AAPL prev_close=304.99, ATM=305, OTM puts: 305, 302.5, 300, 297.5, 295 …
  S1 = 305  (ATM put, nearest)
  S2 = max_OI(302.5, 300)  → whichever has higher OI
  S3 = max_OI(297.5, 295)  → whichever has higher OI

ATM is excluded from resistance (calls) but included in support (puts).

Level semantics
---------------
  RESISTANCE  (R1/R2/R3)  — anchored by Call OI above ATM
  SUPPORT     (S1/S2/S3)  — anchored by Put  OI below ATM
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
    Compute 6 S/R levels (R1-R3, S1-S3) from a normalised option chain.

    Parameters
    ----------
    chain            : Output of DatabentoClient.get_option_chain() or equivalent.
    underlying_price : 8:20 AM spot price used as the ATM anchor.

    Returns
    -------
    list[dict]  — each dict:
        level_type    : 'SUPPORT' | 'RESISTANCE'
        rank          : 1 | 2 | 3
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

    # ATM = strike closest to spot
    all_strikes = sorted(set(float(c['strike']) for c in all_contracts))
    if not all_strikes:
        return []
    atm_strike = min(all_strikes, key=lambda s: abs(s - spot))

    # Resistance: call strikes strictly above ATM, sorted nearest-first (ASC)
    call_strikes_above = sorted(
        s for s in all_strikes if s > atm_strike
    )

    # Support: put strikes at or below ATM, sorted nearest-first (DESC)
    put_strikes_below = sorted(
        (s for s in all_strikes if s <= atm_strike),
        reverse=True,
    )

    levels: list[dict] = []

    for rank, strike in enumerate(_select_levels(call_strikes_above, all_contracts, 'CALL'), start=1):
        c = _best_contract(all_contracts, strike, 'CALL')
        if c:
            levels.append(_make_level('RESISTANCE', rank, c, chain_expiry))

    for rank, strike in enumerate(_select_levels(put_strikes_below, all_contracts, 'PUT'), start=1):
        c = _best_contract(all_contracts, strike, 'PUT')
        if c:
            levels.append(_make_level('SUPPORT', rank, c, chain_expiry))

    # ── Logging ──────────────────────────────────────────────────────────────
    rl = _side(levels, 'RESISTANCE')
    sl = _side(levels, 'SUPPORT')

    def _fmt(side: list, i: int) -> str:
        if i < len(side):
            return f"{side[i]['strike']:.2f}(OI={side[i]['open_interest']:,})"
        return '-'

    logger.info(
        "OI levels  price=%.4f  ATM=%.4f | "
        "R1=%s  R2=%s  R3=%s | S1=%s  S2=%s  S3=%s",
        spot, atm_strike,
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

def _select_levels(
    ordered_strikes: list[float],
    all_contracts: list[dict],
    opt_type: str,
) -> list[float]:
    """
    Return 3 strikes using the window rule:
      level 1 → ordered_strikes[0]  (nearest, always)
      level 2 → highest OI of ordered_strikes[1:3]  (next pair)
      level 3 → highest OI of ordered_strikes[3:5]  (pair after that)
    """
    chosen: list[float] = []

    # Level 1: nearest
    if len(ordered_strikes) >= 1:
        chosen.append(ordered_strikes[0])

    # Levels 2 and 3: non-overlapping pairs
    windows = [ordered_strikes[1:3], ordered_strikes[3:5]]
    for window in windows:
        if not window:
            break
        best_strike = max(
            window,
            key=lambda s: _oi_at(all_contracts, s, opt_type),
        )
        chosen.append(best_strike)

    return chosen


def _oi_at(contracts: list[dict], strike: float, opt_type: str) -> int:
    """Return the highest OI at a given strike and option type."""
    return max(
        (c.get('open_interest', 0)
         for c in contracts
         if float(c['strike']) == strike and c['option_type'] == opt_type),
        default=0,
    )


def _best_contract(contracts: list[dict], strike: float, opt_type: str) -> Optional[dict]:
    """Return the highest-OI contract at the given strike and option type."""
    matching = [
        c for c in contracts
        if float(c['strike']) == strike
        and c['option_type'] == opt_type
        and c.get('open_interest', 0) > 0
    ]
    return max(matching, key=lambda c: c.get('open_interest', 0)) if matching else None


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

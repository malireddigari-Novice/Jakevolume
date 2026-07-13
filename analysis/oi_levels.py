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


def atm_0dte(chain: dict, underlying_price: float, otm_steps: int = 1) -> dict:
    """
    Capture the front (0DTE) ATM window on BOTH sides — the AT-THE-MONEY strike plus the
    next `otm_steps` OUT-OF-THE-MONEY strikes (calls above spot, puts below): e.g. at spot
    ~314 → 315C + 317.5C and 315P + 312.5P. Strike + premium (bid/ask/mark) AND open
    interest per contract. The ATM is chosen by proximity to spot (not OI).

    Returns {'expiry', 'call': [ATM, OTM1, ...], 'put': [ATM, OTM1, ...]} — ATM first,
    then progressively-OTM strikes; either list may be empty if that side has no quotes.
    """
    def _mk(c):
        return {'strike': float(c['strike']), 'bid': c.get('bid'), 'ask': c.get('ask'),
                'mark': c.get('mark'), 'open_interest': c.get('open_interest')}

    def _side(contracts, otm_above: bool):
        cs = sorted((c for c in (contracts or []) if c.get('strike') is not None),
                    key=lambda x: float(x['strike']))
        if not cs:
            return []
        atm_i = min(range(len(cs)), key=lambda i: abs(float(cs[i]['strike']) - underlying_price))
        step = 1 if otm_above else -1                 # OTM = above for calls, below for puts
        out = [_mk(cs[atm_i])]
        i = atm_i + step
        while len(out) <= otm_steps and 0 <= i < len(cs):
            out.append(_mk(cs[i]))
            i += step
        return out

    return {'expiry': chain.get('expiry'),
            'call': _side(chain.get('calls'), otm_above=True),
            'put':  _side(chain.get('puts'),  otm_above=False)}


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


def compute_secondary_watchlist(
    chain: dict,
    underlying_price: float,
    oi_changes: dict | None = None,
) -> list[dict]:
    """
    §11-§16 Secondary OI Watchlist — three tiers beyond the primary S1-R3 levels.

    Tier 1  EXTENDED_RANK  : ranks 4+ within the primary ±OI_LEVEL_BAND_PCT, by OI.
    Tier 2  OUTER_WALL     : top-N by OI in the outer band (primary → SECONDARY_OUTER_BAND_PCT).
    Tier 3  OI_BUILDUP     : top-N by overnight oi_change across ALL strikes with positive
                             OI change (new institutional positioning anywhere in the chain).

    Parameters
    ----------
    chain            : normalised option chain (same shape as compute_oi_levels input)
    underlying_price : 8:20 AM spot (Spot0)
    oi_changes       : {(strike_float, option_type): {'oi_change': int, 'oi_change_pct': float}}
                       queried from option_chain_snapshots after reconcile_oi_changes;
                       None → OI_BUILDUP tier is skipped (first session, no prior data).
    """
    all_contracts = chain.get('all', [])
    if not all_contracts:
        return []

    spot         = underlying_price
    chain_expiry = chain.get('expiry')
    primary_band = config.OI_LEVEL_BAND_PCT
    outer_band   = config.SECONDARY_OUTER_BAND_PCT

    primary_call_hi = spot * (1 + primary_band)
    primary_put_lo  = spot * (1 - primary_band)
    outer_call_hi   = spot * (1 + outer_band)
    outer_put_lo    = spot * (1 - outer_band)

    result: list[dict] = []

    # ── Tier 1: Extended ranks within the primary band ────────────────────
    call_in_primary = [c for c in all_contracts
                       if c['option_type'] == 'CALL'
                       and spot <= float(c['strike']) <= primary_call_hi]
    put_in_primary  = [c for c in all_contracts
                       if c['option_type'] == 'PUT'
                       and primary_put_lo <= float(c['strike']) <= spot]

    total_n = config.TOP_N_LEVELS + config.SECONDARY_WATCHLIST_TOP_N
    for rank, c in enumerate(_top_by_oi(call_in_primary, total_n), start=1):
        if rank <= config.TOP_N_LEVELS:
            continue
        result.append(_make_secondary(c, 'EXTENDED_RANK', 'CALL',
                                      rank - config.TOP_N_LEVELS, spot, chain_expiry, oi_changes))

    for rank, c in enumerate(_top_by_oi(put_in_primary, total_n), start=1):
        if rank <= config.TOP_N_LEVELS:
            continue
        result.append(_make_secondary(c, 'EXTENDED_RANK', 'PUT',
                                      rank - config.TOP_N_LEVELS, spot, chain_expiry, oi_changes))

    # ── Tier 2: Outer-wall strikes (beyond primary band, within outer band) ─
    call_outer = [c for c in all_contracts
                  if c['option_type'] == 'CALL'
                  and primary_call_hi < float(c['strike']) <= outer_call_hi]
    put_outer  = [c for c in all_contracts
                  if c['option_type'] == 'PUT'
                  and outer_put_lo <= float(c['strike']) < primary_put_lo]

    for rank, c in enumerate(_top_by_oi(call_outer, config.SECONDARY_OUTER_TOP_N), start=1):
        result.append(_make_secondary(c, 'OUTER_WALL', 'CALL', rank, spot, chain_expiry, oi_changes))

    for rank, c in enumerate(_top_by_oi(put_outer, config.SECONDARY_OUTER_TOP_N), start=1):
        result.append(_make_secondary(c, 'OUTER_WALL', 'PUT', rank, spot, chain_expiry, oi_changes))

    # ── Tier 3: OI buildup standouts (biggest positive oi_change) ────────
    if oi_changes:
        buildup: list[tuple] = []
        seen: set = set()
        for c in all_contracts:
            key = (float(c['strike']), c['option_type'])
            if key in seen:
                continue
            ch = oi_changes.get(key)
            if ch and (ch.get('oi_change') or 0) > 0:
                buildup.append((ch['oi_change'], c, ch))
                seen.add(key)
        buildup.sort(key=lambda x: x[0], reverse=True)
        for rank, (_, c, _ch) in enumerate(buildup[:config.SECONDARY_OI_BUILDUP_TOP_N], start=1):
            result.append(_make_secondary(c, 'OI_BUILDUP', c['option_type'],
                                          rank, spot, chain_expiry, oi_changes))

    t1 = sum(1 for r in result if r['watchlist_tier'] == 'EXTENDED_RANK')
    t2 = sum(1 for r in result if r['watchlist_tier'] == 'OUTER_WALL')
    t3 = sum(1 for r in result if r['watchlist_tier'] == 'OI_BUILDUP')
    logger.info(
        "Secondary watchlist  extended=%d  outer_wall=%d  oi_buildup=%d",
        t1, t2, t3,
    )
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_secondary(
    contract: dict,
    tier: str,
    option_type: str,
    band_rank: int,
    spot: float,
    chain_expiry,
    oi_changes: dict | None,
) -> dict:
    strike = float(contract['strike'])
    ch = (oi_changes or {}).get((strike, option_type), {})
    return {
        'watchlist_tier': tier,
        'strike':         strike,
        'option_type':    option_type,
        'open_interest':  int(contract.get('open_interest', 0)),
        'oi_change':      ch.get('oi_change'),
        'oi_change_pct':  ch.get('oi_change_pct'),
        'distance_pct':   round((strike - spot) / spot, 6) if spot > 0 else None,
        'band_rank':      band_rank,
        'expiry':         contract.get('expiry', chain_expiry),
    }


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

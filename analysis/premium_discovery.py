"""
Premium Discovery Score (PDS) — Gold filter (§13b).

The insight: detecting unusual volume is not enough. A large volume spike near a
contract's historical premium LOWS after little prior participation is far more
likely to be fresh institutional positioning than a spike in a contract whose
premium has already been discovered (traded heavily at richer levels). The latter
is ambiguous — profit-taking, closing, rolling, or dealer inventory all look the
same on the tape — so it should NOT earn a Gold alert even when volume is high.

This module is pure: it takes a contract's historical premium/volume distribution
(one entry per prior bar: low/high/close/volume) plus the current mark and the
event's trigger volume, and returns a classification + the metrics behind it. No
DB, no network, no config side effects beyond reading threshold constants.

Classes (first match wins):
  VIRGIN_DISCOVERY   — near-zero prior participation anywhere; first price discovery.
  FRESH_ACCUMULATION — low prior participation, mark cheap, and this event is the
                       majority of the contract's all-time volume (new buying).
  ACCEPTED_VALUE     — the current premium region has been frequently traded.
  REPRICED_RECYCLED  — significant historical volume sits ABOVE the current mark;
                       the premium was already discovered richer → ambiguous.
  EXHAUSTED          — mark is well above the historical premium range.

Only VIRGIN_DISCOVERY and FRESH_ACCUMULATION are Gold-eligible.

Why REPRICED_RECYCLED inverts the naive "cheap" read: a contract that traded heavily
at $4-6 and now prints at $3.55 looks maximally cheap to a min/max range gate
((mark-low)/span ≈ 0), so today's §13 percentile PASSES it enthusiastically. PDS
instead sees that most historical volume traded ABOVE $3.55 and rejects it. The
heavy prior participation is exactly the information a min/max range throws away.
"""
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)

VIRGIN_DISCOVERY   = 'VIRGIN_DISCOVERY'
FRESH_ACCUMULATION = 'FRESH_ACCUMULATION'
ACCEPTED_VALUE     = 'ACCEPTED_VALUE'
REPRICED_RECYCLED  = 'REPRICED_RECYCLED'
EXHAUSTED          = 'EXHAUSTED'
# Returned when there is no usable history and PDS_REQUIRE_HISTORY is False — the
# caller treats this as non-blocking (unknown, not a rejection).
UNKNOWN_INSUFFICIENT_HISTORY = 'UNKNOWN_INSUFFICIENT_HISTORY'

_ELIGIBLE = {VIRGIN_DISCOVERY, FRESH_ACCUMULATION}


def is_gold_eligible(pds_class: Optional[str]) -> bool:
    """Only VIRGIN_DISCOVERY / FRESH_ACCUMULATION qualify. Two "no data" states —
    an explicit UNKNOWN_INSUFFICIENT_HISTORY and None (PDS never evaluated: gate off,
    no mark, or a path that doesn't compute it, e.g. chain-led) — are non-blocking
    unless config.PDS_REQUIRE_HISTORY is set, so the gate never silently kills a
    signal it simply couldn't score."""
    if pds_class in (None, UNKNOWN_INSUFFICIENT_HISTORY):
        return not config.PDS_REQUIRE_HISTORY
    return pds_class in _ELIGIBLE


def _bar_mid(bar: dict) -> Optional[float]:
    lo, hi = bar.get('low'), bar.get('high')
    if lo is None or hi is None or lo <= 0 or hi <= 0:
        c = bar.get('close')
        return float(c) if c and c > 0 else None
    return (float(lo) + float(hi)) / 2.0


def score(bars: list, mark: float, event_volume: int) -> Optional[dict]:
    """
    Classify a contract's premium discovery state.

    bars          : prior-session history, each {low, high, close, volume}. MUST
                    exclude the current event bar/session (caller passes history
                    strictly before today).
    mark          : current option mark (premium).
    event_volume  : the trigger volume of the current event (contracts).

    Returns None when the inputs are unusable (no mark). With no usable history it
    returns an UNKNOWN_INSUFFICIENT_HISTORY dict rather than None, so the caller can
    distinguish "not evaluable at all" from "evaluable but no prior participation".
    """
    if not mark or mark <= 0:
        return None

    usable = [b for b in (bars or [])
              if _bar_mid(b) is not None and (b.get('volume') or 0) >= 0]
    if not usable:
        return {'pds_class': UNKNOWN_INSUFFICIENT_HISTORY, 'cum_vol': 0,
                'price_pctile': None, 'vol_at_current': None, 'vol_above': None,
                'time_at_current': None, 'event_share': None, 'eligible': None}

    mids   = [_bar_mid(b) for b in usable]
    vols   = [int(b.get('volume') or 0) for b in usable]
    lo_hist = min(min(float(b['low']) for b in usable if b.get('low') and b['low'] > 0),
                  min(mids))
    hi_hist = max(max(float(b['high']) for b in usable if b.get('high') and b['high'] > 0),
                  max(mids))
    span    = max(hi_hist - lo_hist, 0.01)
    cum_vol = sum(vols)

    # Current premium range position (same convention as the §13 range gate: 0 = at
    # the historical low, 1 = at the historical high). Low = cheap.
    price_pctile = round((mark - lo_hist) / span, 4)

    # Volume/time that traded WITHIN a band around the current mark, and ABOVE it.
    band = mark * config.PDS_BAND_PCT
    above_floor = mark * (1.0 + config.PDS_ABOVE_MARGIN)
    at_vol = sum(v for m, v in zip(mids, vols) if abs(m - mark) <= band)
    at_cnt = sum(1 for m in mids if abs(m - mark) <= band)
    above_vol = sum(v for m, v in zip(mids, vols) if m >= above_floor)

    denom = cum_vol if cum_vol > 0 else 1
    vol_at_current = round(at_vol / denom, 4)
    vol_above      = round(above_vol / denom, 4)
    time_at_current = round(at_cnt / len(usable), 4)
    ev = int(event_volume or 0)
    event_share = round(ev / (cum_vol + ev), 4) if (cum_vol + ev) > 0 else 0.0

    pds_class = _classify(cum_vol, price_pctile, vol_above, vol_at_current, event_share)

    return {
        'pds_class': pds_class,
        'cum_vol': cum_vol,
        'price_pctile': price_pctile,
        'vol_at_current': vol_at_current,
        'vol_above': vol_above,
        'time_at_current': time_at_current,
        'event_share': event_share,
        'eligible': pds_class in _ELIGIBLE,
    }


def _classify(cum_vol, price_pctile, vol_above, vol_at_current, event_share) -> str:
    # Order matters — first match wins.
    # 1) Almost nothing ever traded → first price discovery.
    if cum_vol <= config.PDS_VIRGIN_MAX_HIST_VOL:
        return VIRGIN_DISCOVERY
    # 2) Mark sits above the historical premium range → chased / already run.
    if price_pctile >= config.PDS_EXHAUSTED_PCTILE:
        return EXHAUSTED
    # 3) Significant volume already traded RICHER than here → premium discovered,
    #    the new print is ambiguous (closing/rolling/recycled). The GOOGL 370C case.
    if vol_above >= config.PDS_RECYCLED_ABOVE_SHARE:
        return REPRICED_RECYCLED
    # 4) The current region itself is a frequently-traded / accepted premium.
    if vol_at_current >= config.PDS_ACCEPTED_SHARE:
        return ACCEPTED_VALUE
    # 5) Cheap AND this event is the majority of the contract's all-time volume →
    #    fresh directional accumulation, not recycled positioning. The GOOGL 370P case.
    if (price_pctile <= config.PDS_FRESH_MAX_PCTILE
            and event_share >= config.PDS_FRESH_MIN_EVENT_SHARE):
        return FRESH_ACCUMULATION
    # 6) Everything else — traded before, not clearly fresh → not Gold-eligible.
    return ACCEPTED_VALUE

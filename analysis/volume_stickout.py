"""
Volume Stickout Score (0..1) — detect option volume that genuinely stands out on
the RIGHT tail of the contract's own volume distribution.

Design intent
-------------
Don't fire on tiny volume (a high ratio over a near-zero baseline is noise) and
don't require one fixed huge number (smaller names should pass if their volume is
truly abnormal). Score 0..1; valid >= 0.75, strong >= 0.85.

RIGHT-TAIL GUARANTEE (explicit, per requirement)
------------------------------------------------
A high score alone is not enough — the bar (or 5-min window) must actually sit on
the upper tail of the contract's distribution, not merely clear a ratio. Encoded as:
  • a symbol-aware ABSOLUTE floor (size, regardless of history), and
  • `right_tail_ok`: SessionPercentile >= 90 (when available) OR VisualDominance >= 1.0
    OR WindowDominance >= 1.0  — i.e. the bar/window is in the top decile of the day
    or as large as the biggest recent bar/window.
ValidVolume requires score AND right_tail_ok AND a contract-low backstop.

History reality
---------------
At signal time most contracts have FEW prior 1-min bars (the spike is the contract
waking up). MedianVol20 / SessionPercentile / 5-min windows therefore degrade
gracefully: with <20 prior bars we drop the percentile term and renormalize, and
lean on RecentRatio + VisualDominance + the 5-bar cluster (computable from few bars)
plus the absolute floor. `components_available` reports which terms were usable.

Pure module: no I/O. Feed it lists of completed 1-min volumes (oldest→newest).
"""
from __future__ import annotations
import statistics as _stats
from typing import Optional, Sequence

# ── Symbol-aware hard noise floor (absolute size; the right-tail size guard) ────
FLOOR_DEFAULT  = {'cur': 100, 'win5': 250}
FLOOR_VOLATILE = {'cur': 250, 'win5': 600}   # NVDA / TSLA
VALID_MIN  = 0.75
STRONG_MIN = 0.85


def _step(value: float, table: list[tuple[float, float]]) -> float:
    """table: (cutoff, score) sorted high→low. First cutoff <= value wins, else 0."""
    for cutoff, score in table:
        if value >= cutoff:
            return score
    return 0.0


_RECENT  = [(8.0, 1.00), (5.0, 0.80), (3.0, 0.60), (2.0, 0.30)]
_VISUAL  = [(1.50, 1.00), (1.00, 0.85), (0.75, 0.70), (0.50, 0.40)]
_PCTILE  = [(99, 1.00), (97, 0.85), (95, 0.70), (90, 0.40)]
_WINDOW  = [(1.50, 1.00), (1.00, 0.85), (0.75, 0.70), (0.50, 0.40)]
_NEARLOW = [(None, None)]  # handled explicitly below


def _score_near_low(dist: Optional[float]) -> float:
    if dist is None:
        return 0.40                       # unknown low → neutral-ish, never blocks here
    if dist <= 1.25: return 1.00
    if dist <= 1.50: return 0.85
    if dist <= 1.75: return 0.70
    if dist <= 2.00: return 0.40
    if dist <= 2.50: return 0.00
    return 0.00


def _cluster_scores(win5: float, prior5m_windows: Sequence[float],
                    last5_vols: Sequence[float], median20: float) -> tuple[float, float, float, int]:
    """Return (score_cluster, score_window_dom, window_dominance, active_bars5)."""
    med5 = _stats.median(prior5m_windows) if prior5m_windows else 0.0
    max5 = max(prior5m_windows) if prior5m_windows else 0.0
    window_ratio5  = win5 / max(med5, 50.0)
    window_dom     = win5 / max(max5, 1.0)
    active_thresh  = max(median20 * 2.0, 50.0)
    active5        = sum(1 for v in last5_vols if v >= active_thresh)

    if   window_ratio5 >= 5.0 and active5 >= 4: sc = 1.00
    elif window_ratio5 >= 4.0 and active5 >= 3: sc = 0.85
    elif window_ratio5 >= 3.0 and active5 >= 3: sc = 0.70
    elif window_ratio5 >= 2.0 and active5 >= 2: sc = 0.40
    else: sc = 0.00
    return sc, _step(window_dom, _WINDOW), window_dom, active5


def compute_stickout(
    current_vol: int,
    prior_vols: Sequence[int],          # completed 1-min vols BEFORE current, oldest→newest
    session_vols: Sequence[int],        # all earlier 1-min vols today (for percentile)
    win5: int,                          # sum of last 5 completed bars (incl. current)
    last5_vols: Sequence[int],          # the last 5 bar vols (incl. current)
    prior5m_windows: Sequence[float],   # earlier rolling 5-min window sums today
    contract_low_distance: Optional[float],
    symbol: str,
    volatile_symbols: frozenset = frozenset({'NVDA', 'TSLA'}),
) -> dict:
    """Compute the VolumeStickoutScore and the valid/strong decisions. See module docstring."""
    floor = FLOOR_VOLATILE if symbol in volatile_symbols else FLOOR_DEFAULT

    # ── Hard noise floor (absolute) ────────────────────────────────────────────
    if current_vol < floor['cur'] and win5 < floor['win5']:
        return {'score': 0.0, 'valid': False, 'strong': False,
                'reason': 'BELOW_VOLUME_FLOOR', 'right_tail_ok': False,
                'components_available': 'floor'}

    p20 = list(prior_vols)[-20:]
    n_prior = len(p20)
    median20 = _stats.median(p20) if p20 else 0.0
    avg20    = (sum(p20) / len(p20)) if p20 else 0.0
    max20    = max(p20) if p20 else 0.0
    baseline = max(median20, avg20 * 0.50, 10.0)

    recent_ratio    = current_vol / baseline
    visual_dom      = current_vol / max(max20, 1.0)
    has_pctile      = len(session_vols) >= 20
    if has_pctile:
        le = sum(1 for v in session_vols if v <= current_vol)
        session_pctile = 100.0 * le / len(session_vols)
    else:
        session_pctile = None

    s_recent = _step(recent_ratio, _RECENT)
    s_visual = _step(visual_dom,   _VISUAL)
    s_pctile = _step(session_pctile, _PCTILE) if session_pctile is not None else None
    s_near   = _score_near_low(contract_low_distance)

    s_cluster, s_windowdom, window_dom, active5 = _cluster_scores(
        win5, prior5m_windows, last5_vols, median20)
    cluster_stickout = 0.70 * s_cluster + 0.30 * s_windowdom

    # ── Single-bar score (drop percentile + renormalize when unavailable) ──────
    if s_pctile is not None:
        single = 0.35 * s_recent + 0.30 * s_visual + 0.20 * s_pctile + 0.15 * s_near
        cluster_final = (0.40 * cluster_stickout + 0.25 * s_recent
                         + 0.20 * s_pctile + 0.15 * s_near)
    else:
        # renormalize the non-percentile weights to sum to 1.0
        single = (0.4375 * s_recent + 0.3750 * s_visual + 0.1875 * s_near)
        cluster_final = (0.50 * cluster_stickout + 0.3125 * s_recent + 0.1875 * s_near)

    score = max(single, cluster_final)

    # ── Right-tail requirement (the explicit "unusually high" guard) ───────────
    right_tail_ok = ((session_pctile is not None and session_pctile >= 90)
                     or visual_dom >= 1.00 or window_dom >= 1.00)
    strong_tail = ((session_pctile is not None and session_pctile >= 95)
                   or visual_dom >= 1.50 or (window_dom >= 1.50 and active5 >= 4))

    low_ok_valid  = (contract_low_distance is None or contract_low_distance <= 2.00)
    low_ok_strong = (contract_low_distance is None or contract_low_distance <= 1.75)

    valid  = score >= VALID_MIN  and right_tail_ok and low_ok_valid
    strong = score >= STRONG_MIN and strong_tail   and low_ok_strong

    return {
        'score': round(score, 4),
        'valid': valid, 'strong': strong,
        'right_tail_ok': right_tail_ok,
        'recent_ratio': round(recent_ratio, 2), 's_recent': s_recent,
        'visual_dom': round(visual_dom, 2),     's_visual': s_visual,
        'session_pctile': (round(session_pctile, 1) if session_pctile is not None else None),
        's_pctile': s_pctile,
        'window_dom': round(window_dom, 2), 's_windowdom': s_windowdom,
        's_cluster': s_cluster, 'active5': active5,
        's_near': s_near, 'contract_low_distance': contract_low_distance,
        'baseline': round(baseline, 1), 'n_prior': n_prior,
        'components_available': ('full' if s_pctile is not None
                                 else f'no_pctile(prior={n_prior})'),
        'reason': 'OK' if valid else ('LOW_SCORE' if score < VALID_MIN
                                      else ('NOT_RIGHT_TAIL' if not right_tail_ok
                                            else 'CONTRACT_TOO_HIGH')),
    }

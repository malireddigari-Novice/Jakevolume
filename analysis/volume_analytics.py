"""
Volume Analytics — §26-§29, §31, §41.

Post-session analytics (§26-§29, §31) operate on the option_level_bars 1-min
OHLCV history stored for each signal's traded contract.

Per-minute leadership scores (§41) use the same volume_event / _leadership
functions as the Flow Reversal Engine, stored every intraday poll for ALL
symbols (not only open positions).
"""
import math
import logging

import config
from analysis.flow_reversal import volume_event, _leadership
from analysis import economic_flow as _econ

logger = logging.getLogger(__name__)


# ── §26 Multi-timeframe volume aggregation ────────────────────────────────────

def multitf_volumes(history: list) -> dict:
    """
    Aggregate 1-min volumes (list of dicts with 'volume' key, oldest→newest)
    into 2/3/5/10/15/30-min totals and ratios relative to the 30-min window.
    Returns {} when history is empty.
    """
    vols = [int(b.get('volume', 0)) for b in history]
    if not vols:
        return {}

    def _tail(n):
        return sum(vols[-n:]) if len(vols) >= n else None

    v2, v3, v5, v10, v15, v30 = _tail(2), _tail(3), _tail(5), _tail(10), _tail(15), _tail(30)

    def _r(a, b):
        return round(a / b, 2) if (a is not None and b and b > 0) else None

    return {
        'vol_2m': v2, 'vol_3m': v3, 'vol_5m': v5,
        'vol_10m': v10, 'vol_15m': v15, 'vol_30m': v30,
        'ratio_2m':  _r(v2,  v30),
        'ratio_5m':  _r(v5,  v30),
        'ratio_10m': _r(v10, v30),
        'ratio_15m': _r(v15, v30),
        'ratio_30m': 1.0 if v30 else None,
    }


# ── §27 Volume shape classification ──────────────────────────────────────────

def volume_shape_features(history: list) -> dict:
    """
    Classify the volume distribution over the last ≤20 bars into one of:
      ISOLATED_SPIKE  — one bar dominates (HHI ≥ 0.70, burst_ratio ≥ 5)
      STAIRCASE       — monotonically increasing run (staircase_score ≥ 0.70)
      COMPACT_CLUSTER — 2-4 bars dominate (0.40 ≤ HHI < 0.70)
      DISTRIBUTED     — volume spread evenly (HHI < 0.40)

    Returns shape label + underlying metrics for ML use.
    """
    vols = [int(b.get('volume', 0)) for b in history[-20:]]
    total = sum(vols)
    if not vols or total == 0:
        return {'volume_shape': None, 'shape_hhi': None,
                'burst_ratio': None, 'staircase_score': None}

    n = len(vols)
    shares = [v / total for v in vols]
    hhi = round(sum(s ** 2 for s in shares), 4)

    peak = max(vols)
    rest_avg = (total - peak) / max(n - 1, 1)
    burst_ratio = round(peak / max(rest_avg, 1), 2)

    if n > 1:
        rising = sum(1 for i in range(n - 1) if vols[i + 1] > vols[i])
        staircase_score = round(rising / (n - 1), 4)
    else:
        staircase_score = 0.0

    if hhi >= 0.70 and burst_ratio >= 5:
        shape = 'ISOLATED_SPIKE'
    elif staircase_score >= 0.70 and hhi < 0.70:
        shape = 'STAIRCASE'
    elif 0.40 <= hhi < 0.70:
        shape = 'COMPACT_CLUSTER'
    else:
        shape = 'DISTRIBUTED'

    return {'volume_shape': shape, 'shape_hhi': hhi,
            'burst_ratio': burst_ratio, 'staircase_score': staircase_score}


# ── §28 Volume entropy ────────────────────────────────────────────────────────

def normalized_entropy(history: list) -> float | None:
    """
    NormalizedEntropy = -Σ p_i * log(p_i) / log(n)  (last ≤20 bars, n = non-zero bars).
    Range [0, 1]: 0 = all volume in one bar, 1 = perfectly uniform.
    """
    vols = [int(b.get('volume', 0)) for b in history[-20:] if int(b.get('volume', 0)) > 0]
    n = len(vols)
    if n < 2:
        return None
    total = sum(vols)
    if total == 0:
        return None
    ent = -sum((v / total) * math.log(v / total) for v in vols)
    return round(ent / math.log(n), 4)


# ── §29 Chain-relative volume ─────────────────────────────────────────────────

def chain_relative_volume(
    level_bars: list,
    signal_level_type: str,
    signal_rank: int,
    spot: float,
) -> dict:
    """
    Partition today's option-level volume into ATM / ITM / OTM shares.

    level_bars: list of dicts with keys: level_type, rank, strike, option_type, volume.
    These are all S/R level bars for the symbol on the day.

    Classification:
      ATM = the signal's own (level_type, rank) bucket
      ITM = same side, higher rank (deeper into the chain)
      OTM = opposite side (resistances for a bullish, supports for a bearish)
    """
    if not level_bars:
        return {'atm_vol_share': None, 'itm_vol_share': None, 'otm_vol_share': None,
                'strike_volume_center': None, 'center_vs_spot': None}

    vol_by_key: dict[tuple, int] = {}
    for b in level_bars:
        key = (b['level_type'], int(b['rank']))
        vol_by_key[key] = vol_by_key.get(key, 0) + int(b.get('volume', 0))

    total = sum(vol_by_key.values())
    if total == 0:
        return {'atm_vol_share': None, 'itm_vol_share': None, 'otm_vol_share': None,
                'strike_volume_center': None, 'center_vs_spot': None}

    atm_vol = vol_by_key.get((signal_level_type, signal_rank), 0)
    itm_vol = sum(v for (lt, r), v in vol_by_key.items()
                  if lt == signal_level_type and r > signal_rank)
    opp = 'RESISTANCE' if signal_level_type == 'SUPPORT' else 'SUPPORT'
    otm_vol = sum(v for (lt, _), v in vol_by_key.items() if lt == opp)

    def _s(v):
        return round(v / total, 4)

    strike_vols = [(float(b['strike']), int(b.get('volume', 0)))
                   for b in level_bars if int(b.get('volume', 0)) > 0 and b.get('strike')]
    if strike_vols:
        wvol = sum(v for _, v in strike_vols)
        center = round(sum(s * v for s, v in strike_vols) / wvol, 4) if wvol else None
        center_vs_spot = round((center - spot) / spot, 4) if (center and spot > 0) else None
    else:
        center = center_vs_spot = None

    return {
        'atm_vol_share':        _s(atm_vol),
        'itm_vol_share':        _s(itm_vol),
        'otm_vol_share':        _s(otm_vol),
        'strike_volume_center': center,
        'center_vs_spot':       center_vs_spot,
    }


# ── §31 Volume migration direction ────────────────────────────────────────────

def volume_migration(history: list, signal_level_type: str) -> dict:
    """
    Measure whether volume was front- or back-loaded over the session.

    Computes the volume-weighted bar centroid and subtracts the session midpoint:
      vol_center_change > 0 → back-loaded  → APPROACHING_ATM
      vol_center_change < 0 → front-loaded → MOVING_AWAY
      near zero (< 0.5 bar) → STABLE
    """
    vols = [int(b.get('volume', 0)) for b in history]
    n = len(vols)
    total = sum(vols)
    if n < 4 or total == 0:
        return {'vol_center_change': None, 'vol_migration_direction': None}

    centroid = sum(i * v for i, v in enumerate(vols)) / total
    midpoint = (n - 1) / 2.0
    change = round(centroid - midpoint, 4)

    if abs(change) < 0.5:
        direction = 'STABLE'
    elif change > 0:
        direction = 'APPROACHING_ATM'
    else:
        direction = 'MOVING_AWAY'

    return {'vol_center_change': change, 'vol_migration_direction': direction}


# ── §41 Per-minute call/put leadership scores ─────────────────────────────────

def compute_leadership_scores(
    symbol: str,
    option_quotes: dict,
    opt_vol_hist: dict,
    low_dist_fn=None,
    spot=None,
) -> dict | None:
    """
    Compute per-minute call and put leadership scores from all watched contracts.

    Reuses volume_event / _leadership from flow_reversal so the scoring
    formula is identical to the reversal engine but runs for every symbol,
    not only those with open positions.

    Parameters
    ----------
    option_quotes : {(strike, option_type): quote_dict}
    opt_vol_hist  : detector._opt_vol_hist — {(symbol, strike, opt_type): deque}
    low_dist_fn   : optional callable((symbol, strike, opt_type), quote) -> float|None

    Returns a dict ready to be passed to db.save_volume_leadership, or None if
    no option data is available.
    """
    call_events: list[dict] = []
    put_events:  list[dict] = []

    for (strike, ot), q in option_quotes.items():
        hist_key = (symbol, strike, ot)
        hist = list(opt_vol_hist.get(hist_key, []))
        ev = volume_event(hist)
        low_dist = low_dist_fn(hist_key, q) if low_dist_fn else None
        item = {'strike': strike, 'ev': ev, 'low_dist': low_dist, 'side': ot,
                'mark': q.get('mark'),
                'event_vol': (ev['event_vol'] if ev else 0),
                'event_share': (ev['share'] if ev else None)}
        (call_events if ot == 'CALL' else put_events).append(item)

    if not call_events and not put_events:
        return None

    call_ld = _leadership(call_events) if call_events else {'score': 0.0}
    put_ld  = _leadership(put_events)  if put_events  else {'score': 0.0}

    call_score = call_ld['score']
    put_score  = put_ld['score']
    diff = round(call_score - put_score, 4)

    # #5 — economically-weighted leadership: replace the structural (volume-metric)
    # scores with each side's share of $-weighted fresh flow (vol×mark×relevance×
    # concentration), so pennies / far-OTM inventory don't fabricate leadership. Only
    # when the total weight is meaningful; otherwise keep the structural scores (a tiny
    # amount of flow shouldn't read as 100% one-sided). Scores stay on a 0–1 scale, so
    # downstream thresholds (CHAIN_LEADERSHIP_MIN, margins) still apply.
    econ = None
    if config.ECONOMIC_LEADERSHIP_ENABLED:
        econ = _econ.weighted_leadership(call_events + put_events, spot)
        if econ['total'] >= config.ECONOMIC_LEADERSHIP_MIN_TOTAL:
            tot = max(econ['total'], 1)
            call_score = round(econ['call_weight'] / tot, 4)
            put_score  = round(econ['put_weight'] / tot, 4)
            diff = round(call_score - put_score, 4)

    def _vol5(events):
        return sum(e['ev']['event_vol'] for e in events
                   if e.get('ev') and e['ev'].get('event_vol'))

    call_vol5 = _vol5(call_events)
    put_vol5  = _vol5(put_events)

    dominant = ('NEUTRAL' if abs(diff) < 0.05 else
                'CALL'    if diff > 0          else 'PUT')

    return {
        'call_leadership': round(call_score, 4),
        'put_leadership':  round(put_score,  4),
        'leadership_diff': diff,
        'dominant_side':   dominant,
        'call_vol_5m':     call_vol5 or None,
        'put_vol_5m':      put_vol5  or None,
    }

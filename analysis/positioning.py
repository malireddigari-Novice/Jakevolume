"""
Fresh-OI positioning engine (V2 Engine 2 — overnight context, NEVER a trigger).

OI is inventory, not a prediction. What matters is what happened to the inventory since
the prior session, and where NEW risk was placed:

  Fresh OI  = today's OI - prior session's OI  (large fresh changes >> old positioning)
  Inventory state (per contract, from OI change vs volume):
      BUILD    — OI rose: new positions created (real accumulation)
      UNWIND   — OI fell: positions closed (a level may have lost significance)
      ROTATION — flat OI but heavy volume: ownership changed hands, same net OI
      STABLE   — little activity

The heat-map scores each ticker by where institutions placed meaningful NEW risk before
the open — concentration (how tightly fresh OI clusters), directionality (calls vs puts
near spot), distance from spot (2% away >> 20% away), and net notional (contracts x premium
x 100). Live flow decides whether those participants actually take control; this only sets
the battlefield + a confidence weighting. Pure functions.
"""
from typing import Optional


def inventory_state(oi_change, volume, *, build_min: int, unwind_min: int,
                    rotation_vol_min: int, flat_oi_max: int) -> str:
    """Classify one contract's inventory change (BUILD / UNWIND / ROTATION / STABLE)."""
    oc, v = int(oi_change or 0), int(volume or 0)
    if oc >= build_min:
        return 'BUILD'
    if oc <= -unwind_min:
        return 'UNWIND'
    if abs(oc) <= flat_oi_max and v >= rotation_vol_min:
        return 'ROTATION'      # inventory rotated: heavy volume, flat OI
    return 'STABLE'


def _distance_weight(distance_pct: Optional[float], band_pct: float) -> float:
    """Nearer-the-money strikes count more; ~0 beyond the band (a 20%-OTM hedge ≈ noise)."""
    if distance_pct is None or band_pct <= 0:
        return 0.0
    return max(0.0, 1.0 - distance_pct / band_pct)


def build_records(contracts: list, oi_changes: dict, spot: float, *, build_min: int,
                  unwind_min: int, rotation_vol_min: int, flat_oi_max: int) -> list:
    """Per-contract positioning records for contracts with a known fresh-OI change."""
    recs = []
    for c in contracts or []:
        s = float(c['strike']); ot = c.get('option_type')
        oc = (oi_changes.get((s, ot)) or {}).get('oi_change')
        if oc is None:
            continue
        vol = int(c.get('volume') or 0)
        mark = float(c.get('mark') or 0.0)
        dist = (abs(s - spot) / spot) if spot else None
        recs.append({
            'strike': s, 'side': ot, 'fresh_oi': int(oc), 'volume': vol, 'mark': mark,
            'notional': round(abs(int(oc)) * mark * 100.0),
            'state': inventory_state(oc, vol, build_min=build_min, unwind_min=unwind_min,
                                     rotation_vol_min=rotation_vol_min, flat_oi_max=flat_oi_max),
            'distance_pct': dist,
        })
    return recs


def _concentration(fresh: list, spot: float, band_pct: float) -> tuple:
    """Cluster width of the strikes holding ~80% of fresh notional. Returns
    (factor 0-1, label, cluster_low, cluster_high)."""
    if not fresh:
        return 0.0, 'LOW', None, None
    ranked = sorted(fresh, key=lambda r: r['notional'], reverse=True)
    total = sum(r['notional'] for r in ranked) or 1
    acc, core = 0, []
    for r in ranked:
        core.append(r['strike']); acc += r['notional']
        if acc >= 0.8 * total:
            break
    lo, hi = min(core), max(core)
    width_pct = (hi - lo) / spot if spot else 1.0
    factor = max(0.0, 1.0 - width_pct / band_pct) if band_pct > 0 else 0.0
    label = ('VERY_HIGH' if factor >= 0.8 else 'HIGH' if factor >= 0.6
             else 'MODERATE' if factor >= 0.35 else 'LOW')
    return round(factor, 3), label, lo, hi


def heatmap(symbol: str, contracts: list, oi_changes: dict, spot: float, *,
            near_band_pct: float = 0.05, target_notional: float = 500_000.0,
            build_min: int = 250, unwind_min: int = 250,
            rotation_vol_min: int = 1000, flat_oi_max: int = 100) -> dict:
    """
    Overnight positioning heat-map for one symbol. Directional scores are 0-10, driven by
    distance-weighted fresh-OI notional (calls bullish / puts bearish), size, and how
    tightly the fresh positioning clusters. Context only — never fires a trade.
    """
    recs = build_records(contracts, oi_changes, spot, build_min=build_min,
                         unwind_min=unwind_min, rotation_vol_min=rotation_vol_min,
                         flat_oi_max=flat_oi_max)
    fresh = [r for r in recs if r['state'] == 'BUILD']

    def _wn(side):
        return sum(r['notional'] * _distance_weight(r['distance_pct'], near_band_pct)
                   for r in fresh if r['side'] == side)
    call_wn, put_wn = _wn('CALL'), _wn('PUT')
    total_wn = call_wn + put_wn
    net_notional = sum(r['notional'] for r in fresh)
    conc_factor, conc_label, clo, chi = _concentration(fresh, spot, near_band_pct)
    size = min(1.0, net_notional / target_notional) if target_notional else 0.0
    call_dom = call_wn / (total_wn + 1.0)
    put_dom  = put_wn / (total_wn + 1.0)

    def _score(dom):
        return round(10.0 * (0.5 * dom + 0.3 * size + 0.2 * conc_factor), 1)
    bull, bear = _score(call_dom), _score(put_dom)
    dominant = ('CALL' if call_wn > put_wn * 1.2 else 'PUT' if put_wn > call_wn * 1.2
                else 'NEUTRAL')
    wdist = (sum(r['notional'] * (r['distance_pct'] or 0) for r in fresh) / net_notional
             if net_notional else None)
    top = sorted(fresh, key=lambda r: r['notional'], reverse=True)[:5]

    return {
        'symbol': symbol, 'spot': spot,
        'dominant_side': dominant, 'bull_score': bull, 'bear_score': bear,
        'net_notional': net_notional, 'call_notional': round(call_wn), 'put_notional': round(put_wn),
        'concentration': conc_label, 'concentration_factor': conc_factor,
        'cluster_low': clo, 'cluster_high': chi,
        'cluster_width': (round(chi - clo, 2) if clo is not None else None),
        'weighted_distance_pct': (round(wdist, 4) if wdist is not None else None),
        'fresh_count': len(fresh),
        'top_strikes': [{'strike': r['strike'], 'side': r['side'], 'fresh_oi': r['fresh_oi'],
                         'notional': r['notional'], 'distance_pct': r['distance_pct'],
                         'state': r['state']} for r in top],
        'unwind': [{'strike': r['strike'], 'side': r['side'], 'fresh_oi': r['fresh_oi']}
                   for r in recs if r['state'] == 'UNWIND'][:5],
    }

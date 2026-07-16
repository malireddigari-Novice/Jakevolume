"""
Chain leadership detection (V2 — leadership as a first-class concept).

Instead of asking "did strike X meet a threshold?", ask "did one side seize control
of the chain?" — measured by COORDINATED participation across many strikes, not a single
print. This is what a "one event spread across five strikes" (e.g. GOOGL 357.5→365C each
printing a few hundred) looks like: no single strike clears a 1,000-contract floor, but
together they are unmistakable call leadership.

detect() separates the three decisions the way the trader reasons:
  1. Did a side seize the chain?      -> controlling_side (+ confidence)
  2. Which strike is the leader?      -> leader_strike (highest notional participation)
  3. Which contract best expresses it -> recommended_strike (convexity: the furthest
                                         still-well-supported OTM strike)

Pure functions over per-side contract lists. Each contract dict:
    {'strike': float, 'vol': int, 'notional': float, 'mark': float, 'low_dist': float|None}
`vol` is the event volume over the measurement window (e.g. 1-3 min), `notional` =
contracts * premium * 100. Thresholds are passed in (the caller supplies config values),
so the engine is testable in isolation.
"""
from typing import Optional


def window_n(symbol: str, next_day_mode: bool, per_symbol: dict,
             default: int, nextday_bonus: int) -> int:
    """Adaptive nearest-N strikes per side for the watched set — per underlying, wider for
    next-day (1DTE) flow. Fast movers (NVDA/TSLA) reach farther than AAPL/MSFT."""
    n = int(per_symbol.get(symbol, default))
    return n + (nextday_bonus if next_day_mode else 0)


def _side_summary(contracts: list, strike_min_vol: int) -> dict:
    """Aggregate one side's participating strikes (those clearing the per-strike floor)."""
    part = [c for c in (contracts or []) if (c.get('vol') or 0) >= strike_min_vol]
    return {
        'participating': part,
        'breadth': len(part),
        'combined_volume': sum(int(c.get('vol') or 0) for c in part),
        'combined_notional': sum(float(c.get('notional') or 0) for c in part),
        'strikes': sorted(c['strike'] for c in part),
    }


def _confidence(lead: dict, other: dict, *, min_breadth: int, min_notional: float,
                leadership_margin: float) -> int:
    """0-100 leadership confidence from breadth, notional size, and dominance margin."""
    breadth_term = min(lead['breadth'] / max(min_breadth + 2, 1), 1.0)          # saturates a bit above the floor
    notional_term = min(lead['combined_notional'] / max(min_notional * 3, 1), 1.0)
    dom = lead['combined_notional'] / max(other['combined_notional'], 1.0)
    dom_term = min(max(dom - 1.0, 0.0) / max(leadership_margin * 2, 1), 1.0)
    return int(round(100 * (0.35 * breadth_term + 0.35 * notional_term + 0.30 * dom_term)))


def detect(call_contracts: list, put_contracts: list, spot: float, *,
           strike_min_vol: int = 200, min_breadth: int = 3,
           min_combined_vol: int = 1500, min_notional: float = 100_000.0,
           leadership_margin: float = 1.5, convexity_min_frac: float = 0.4) -> dict:
    """
    Detect chain-wide CALL/PUT leadership. Returns a verdict dict:
      controlling_side, confidence, leader_strike, recommended_strike, supporting_strikes,
      breadth, combined_volume, combined_notional, call{summary}, put{summary}, reason.
    controlling_side is None when neither side coordinated enough / neither dominates.
    """
    call = _side_summary(call_contracts, strike_min_vol)
    put  = _side_summary(put_contracts, strike_min_vol)

    def _qualifies(s):
        return (s['breadth'] >= min_breadth
                and s['combined_volume'] >= min_combined_vol
                and s['combined_notional'] >= min_notional)

    call_ok, put_ok = _qualifies(call), _qualifies(put)
    controlling, lead, other = None, None, None
    if call_ok and call['combined_notional'] >= leadership_margin * max(put['combined_notional'], 1.0):
        controlling, lead, other = 'CALL', call, put
    elif put_ok and put['combined_notional'] >= leadership_margin * max(call['combined_notional'], 1.0):
        controlling, lead, other = 'PUT', put, call

    base = {'call': call, 'put': put, 'leader_strike': None, 'recommended_strike': None,
            'supporting_strikes': [], 'breadth': 0, 'combined_volume': 0,
            'combined_notional': 0.0, 'confidence': 0}

    if controlling is None:
        if not (call_ok or put_ok):
            reason = 'NO_COORDINATED_SIDE'          # neither side had enough breadth/notional
        else:
            reason = 'NO_DOMINANT_SIDE'             # a side coordinated but did not dominate the other
        return {**base, 'controlling_side': None, 'reason': reason}

    part = lead['participating']
    leader = max(part, key=lambda c: float(c.get('notional') or 0))
    leader_strike = leader['strike']
    leader_vol = leader.get('vol') or 0

    # Recommended = the convexity step just BEYOND the leader: the nearest still-well-
    # supported strike one step more OTM than the leader (e.g. leader 360C → recommend
    # 362.5C). Not the furthest (which is the most chased/lottery). Falls back to leader.
    beyond = [c for c in part
              if ((c['strike'] > leader_strike) if controlling == 'CALL' else (c['strike'] < leader_strike))
              and (c.get('vol') or 0) >= convexity_min_frac * leader_vol]
    rec = min(beyond, key=lambda c: abs(c['strike'] - leader_strike)) if beyond else leader

    return {
        'controlling_side': controlling,
        'confidence': _confidence(lead, other, min_breadth=min_breadth,
                                  min_notional=min_notional, leadership_margin=leadership_margin),
        'leader_strike': leader_strike,
        'recommended_strike': rec['strike'],
        'supporting_strikes': lead['strikes'],
        'breadth': lead['breadth'],
        'combined_volume': lead['combined_volume'],
        'combined_notional': lead['combined_notional'],
        'call': call, 'put': put, 'reason': 'LEADERSHIP_CONFIRMED',
    }

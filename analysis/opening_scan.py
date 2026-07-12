"""
Opening ATM±N event-time scan (P-ET step 5).

During the opening window, surface contracts that crossed the production floor at
EVENT time and were within ATM ± OPENING_STRIKE_WINDOW strikes AT THAT MOMENT — using
the frozen event-time distance, so a contract that flowed at the open and then ran
ITM/OTM before the bar closed stays eligible (the TSLA-425P failure). Unlike the
level path, this looks across the nearby chain, not only S/R-level contracts.

Research-only for now: scan_opening() returns eligible candidates for logging/audit; it
does NOT auto-fire. Promotion to production goes through the Gold gate after the P6
control tests (AAPL 310C breakout, TSLA 425P opening) validate it.
"""
from typing import Optional


def classify_opening_story(*, call_vol, put_vol, call_prem_chg, put_prem_chg,
                           call_lead, put_lead, spot_chg) -> str:
    """
    §9/§11 opening directional story from both-sided chain aggregates. Distinguishes
    directional demand from supply/non-directional flow — a large put event with fading
    put premium while spot rises and calls lead is PUT_SUPPLY_BULLISH, not put demand.
    """
    cv, pv = (call_vol or 0), (put_vol or 0)
    call_demand = call_lead >= put_lead and call_prem_chg >= 0 and spot_chg >= 0
    put_demand  = put_lead >= call_lead and put_prem_chg >= 0 and spot_chg <= 0
    # Supply requires the side to be the LARGER (dominant-volume) event whose premium
    # is nonetheless fading while spot moves against it and the other side leads.
    put_supply  = pv >= cv and put_prem_chg < 0 and spot_chg > 0 and call_lead > put_lead
    call_supply = cv >= pv and call_prem_chg < 0 and spot_chg < 0 and put_lead > call_lead
    if put_supply:
        return 'OPENING_PUT_SUPPLY_BULLISH'
    if call_supply:
        return 'OPENING_CALL_SUPPLY_BEARISH'
    if call_demand and not put_demand:
        return 'OPENING_CALL_DEMAND_DOMINANT'
    if put_demand and not call_demand:
        return 'OPENING_PUT_DEMAND_DOMINANT'
    if not call_demand and not put_demand:
        return 'OPENING_NO_CONVICTION'
    return 'OPENING_MIXED'


def strike_increment(option_quotes: dict, default: float = 2.5) -> float:
    """Smallest positive gap between adjacent strikes in the chain (fallback `default`)."""
    ks = sorted({float(s) for (s, _ot) in option_quotes})
    diffs = [round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a]
    return min(diffs) if diffs else default


def event_time_eligible(event_state, window_strikes: int, increment: float) -> bool:
    """
    True iff the contract crossed the production floor AND was within ATM ± window
    strikes AT EVENT TIME (frozen), regardless of where spot/ATM are now.
    """
    if event_state is None or not event_state.crossed:
        return False
    d = event_state.strike_distance_strikes(increment)
    return d is not None and d <= window_strikes


def opening_side_confirmed(option_type: str, story: str) -> bool:
    """
    Fix (2) directional safety gate: a promotable opening candidate must be on the
    demand-dominant side of the both-sided story. A large PUT print is only promotable
    when the story is OPENING_PUT_DEMAND_DOMINANT — never on put-supply-bullish,
    no-conviction, or mixed. This is the single most important gate for the opening path.
    """
    if option_type == 'CALL':
        return story == 'OPENING_CALL_DEMAND_DOMINANT'
    if option_type == 'PUT':
        return story == 'OPENING_PUT_DEMAND_DOMINANT'
    return False


def opening_story(symbol: str, option_quotes: dict, event_reg, leadership: Optional[dict],
                  close_price: float, spot_open: Optional[float]) -> str:
    """
    Build the both-sided opening directional story from the ATM call/put event state.
    Volume + premium-change come from the FROZEN event-time quotes (final revised volume
    and last_at_threshold mark vs the current mark); leadership + spot change give the
    directional context. Returns a classify_opening_story() label.
    """
    calls = [s for (s, ot) in option_quotes if ot == 'CALL']
    puts  = [s for (s, ot) in option_quotes if ot == 'PUT']

    def _atm(strikes):
        return min(strikes, key=lambda s: abs(s - close_price)) if strikes else None

    def _vol_premchg(strike, ot):
        if strike is None:
            return 0, 0.0
        es = event_reg.get(symbol, float(strike), ot)
        cur = (option_quotes.get((strike, ot)) or {}).get('mark')
        if es is not None and es.crossed:
            vol = es.final_revised_volume or es.r180_at_threshold or 0
            base = es.last_at_threshold
            prem_chg = (float(cur) - float(base)) if (cur and base) else 0.0
            return vol, prem_chg
        return 0, 0.0

    ac, ap = _atm(calls), _atm(puts)
    cv, cpc = _vol_premchg(ac, 'CALL')
    pv, ppc = _vol_premchg(ap, 'PUT')
    cl = (leadership or {}).get('call_leadership', 0.0)
    pl = (leadership or {}).get('put_leadership', 0.0)
    spot_chg = (close_price - spot_open) if spot_open else 0.0
    return classify_opening_story(call_vol=cv, put_vol=pv, call_prem_chg=cpc,
                                  put_prem_chg=ppc, call_lead=cl, put_lead=pl,
                                  spot_chg=spot_chg)


def scan_opening(symbol: str, option_quotes: dict, event_reg,
                 *, window_strikes: int, increment: Optional[float] = None) -> list:
    """
    Return event-time-eligible, floor-crossed contracts across the nearby chain for the
    opening window. Each item: {symbol, strike, option_type, event_state, no_retro,
    dist_strikes}. Purely reads the registry — no firing decision here.
    """
    incr = increment if increment else strike_increment(option_quotes)
    out = []
    for (strike, otype) in option_quotes:
        es = event_reg.get(symbol, float(strike), otype)
        if event_time_eligible(es, window_strikes, incr):
            out.append({
                'symbol': symbol, 'strike': float(strike), 'option_type': otype,
                'event_state': es, 'no_retro': es.no_retro_label(),
                'dist_strikes': es.strike_distance_strikes(incr),
            })
    return out

"""
Directional-intent validation + opposite-side veto (§5-§9) — Gold-mode P2.

Volume proves that *something* happened; it does not prove contracts were bought
directionally (§5). After a qualifying volume event we watch the event bar plus the
next 1-3 completed 1-min bars and ask whether the response is consistent with
directional DEMAND vs option SUPPLY / non-directional flow (§6-§8), and whether the
OPPOSITE side of the nearby chain shows materially stronger validated demand (§9).

Only LIKELY_DIRECTIONAL_CALL_DEMAND / LIKELY_DIRECTIONAL_PUT_DEMAND may support a Gold
production entry; everything else is research-only.

An "observation" (obs) is a plain dict captured at a point in time for the traded
contract and its side context:
    {'mark': float, 'iv': float|None, 'spot': float,
     'call_leadership': float, 'put_leadership': float}
"""
import logging

import config

logger = logging.getLogger(__name__)

# §8 outcomes
LIKELY_DIRECTIONAL_CALL_DEMAND = 'LIKELY_DIRECTIONAL_CALL_DEMAND'
LIKELY_DIRECTIONAL_PUT_DEMAND  = 'LIKELY_DIRECTIONAL_PUT_DEMAND'
PROBABLE_CALL_SUPPLY           = 'PROBABLE_CALL_SUPPLY'
PROBABLE_PUT_SUPPLY            = 'PROBABLE_PUT_SUPPLY'
POSSIBLE_BUYER_EXIT            = 'POSSIBLE_BUYER_EXIT'
POSSIBLE_SELLER_COVER          = 'POSSIBLE_SELLER_COVER'
POSSIBLE_SPREAD_OR_HEDGE       = 'POSSIBLE_SPREAD_OR_HEDGE'
MIXED_OR_UNKNOWN               = 'MIXED_OR_UNKNOWN'

_DEMAND = {LIKELY_DIRECTIONAL_CALL_DEMAND, LIKELY_DIRECTIONAL_PUT_DEMAND}

VETO_OPPOSITE_DOMINANT = 'OPPOSITE_SIDE_DIRECTIONAL_DEMAND_DOMINANT'


def is_directional_demand(intent_class: str) -> bool:
    """Only the two LIKELY_DIRECTIONAL_*_DEMAND outcomes may support a Gold entry (§8)."""
    return intent_class in _DEMAND


def _same_opp(side: str, obs: dict):
    """Return (same_side_leadership, opposite_side_leadership) for the candidate side."""
    call_ld = obs.get('call_leadership', 0.0) or 0.0
    put_ld  = obs.get('put_leadership', 0.0) or 0.0
    return (call_ld, put_ld) if side == 'CALL' else (put_ld, call_ld)


def classify_intent(side: str, event: dict, followups: list) -> str:
    """
    Classify directional intent from the event bar + 1-3 follow-up observations (§6-§8).

    side       : 'CALL' | 'PUT' (the candidate's directional side)
    event      : observation at the event bar
    followups  : list of later observations (chronological); may be empty

    Returns one of the §8 outcomes. With no follow-ups yet, returns MIXED_OR_UNKNOWN
    (undecided) so the caller keeps waiting rather than firing.
    """
    if not followups:
        return MIXED_OR_UNKNOWN
    last = followups[-1]
    ev_mark = event.get('mark') or 0.0
    prem_chg = ((last.get('mark') or 0.0) - ev_mark) / ev_mark if ev_mark > 0 else 0.0
    ev_iv, last_iv = event.get('iv'), last.get('iv')
    iv_chg = (last_iv - ev_iv) if (ev_iv is not None and last_iv is not None) else None
    ev_spot = event.get('spot') or 0.0
    spot_chg = ((last.get('spot') or 0.0) - ev_spot) / ev_spot if ev_spot > 0 else 0.0
    same_ld, opp_ld = _same_opp(side, last)

    hold      = config.INTENT_PREMIUM_HOLD_PCT           # e.g. -0.10 (may dip 10%)
    contra    = config.INTENT_SPOT_CONTRADICT_PCT        # e.g. 0.003
    prem_holds = prem_chg >= hold
    iv_ok      = (iv_chg is None) or (iv_chg >= 0)
    lead_ok    = same_ld >= opp_ld

    if side == 'CALL':
        spot_supports    = spot_chg >= -contra       # not falling hard
        spot_contradicts = spot_chg < 0
        if prem_holds and iv_ok and spot_supports and lead_ok:
            return LIKELY_DIRECTIONAL_CALL_DEMAND
        if (not prem_holds) and spot_contradicts and opp_ld > same_ld:
            return PROBABLE_CALL_SUPPLY
        # premium fades but spot still up → holders taking profit / closing
        if (not prem_holds) and spot_chg > contra:
            return POSSIBLE_BUYER_EXIT
        return MIXED_OR_UNKNOWN
    else:  # PUT — bearish thesis
        spot_supports    = spot_chg <= contra        # not rising hard
        spot_contradicts = spot_chg > 0
        if prem_holds and iv_ok and spot_supports and lead_ok:
            return LIKELY_DIRECTIONAL_PUT_DEMAND
        if (not prem_holds) and spot_contradicts and opp_ld > same_ld:
            return PROBABLE_PUT_SUPPLY
        if (not prem_holds) and spot_chg < -contra:
            return POSSIBLE_BUYER_EXIT
        return MIXED_OR_UNKNOWN


def opposite_side_veto(side: str, obs: dict, prem_chg: float = 0.0) -> str:
    """
    §9 opposite-side leadership veto. Block a candidate when the OPPOSITE side shows
    materially stronger validated directional demand and the candidate's own thesis
    is failing.

    For a PUT candidate: veto if call leadership exceeds put leadership by
    LEADERSHIP_VETO_MARGIN AND the put premium is not expanding AND spot is rising.
    Mirror for a CALL candidate. Returns VETO_OPPOSITE_DOMINANT or '' (no veto).
    """
    same_ld, opp_ld = _same_opp(side, obs)
    margin_ok = (opp_ld - same_ld) >= config.LEADERSHIP_VETO_MARGIN
    prem_failing = prem_chg < config.INTENT_PREMIUM_HOLD_PCT
    spot = obs.get('spot')
    ev_spot = obs.get('event_spot', spot)
    spot_chg = ((spot or 0.0) - (ev_spot or 0.0)) / ev_spot if ev_spot else 0.0
    if side == 'PUT':
        against = spot_chg > config.INTENT_SPOT_CONTRADICT_PCT   # spot rising against a put
    else:
        against = spot_chg < -config.INTENT_SPOT_CONTRADICT_PCT  # spot falling against a call
    return VETO_OPPOSITE_DOMINANT if (margin_ok and prem_failing and against) else ''


class IntentValidator:
    """
    Per-symbol deferred-confirmation buffer. A Gold candidate registers its event
    observation; each subsequent poll feeds a follow-up observation; once at least
    INTENT_CONFIRMATION_BARS_MIN follow-ups are collected the verdict is available.
    Candidates expire after INTENT_CONFIRMATION_BARS_MAX follow-ups.
    """

    def __init__(self) -> None:
        self._pending: dict = {}   # (symbol, side, strike) -> {event, followups, payload}

    def register(self, symbol: str, side: str, strike: float,
                 event_obs: dict, payload=None) -> None:
        self._pending[(symbol, side, float(strike))] = {
            'event': event_obs, 'followups': [], 'payload': payload}

    def pending_items(self, symbol: str) -> list:
        """(side, strike) of all pending candidates for `symbol` (snapshot copy)."""
        return [(s, k) for (sym, s, k) in list(self._pending) if sym == symbol]

    def observe(self, symbol: str, side: str, strike: float, obs: dict) -> dict:
        """
        Append a follow-up observation and return the current status:
          {'status': 'PENDING'|'CONFIRMED'|'REJECTED'|'EXPIRED',
           'intent_class': str|None, 'payload', 'event_obs', 'last_obs'}
        CONFIRMED only when a LIKELY_DIRECTIONAL_*_DEMAND is reached within the window.
        Terminal statuses (CONFIRMED/REJECTED/EXPIRED) drop the candidate.
        """
        key = (symbol, side, float(strike))
        p = self._pending.get(key)
        if p is None:
            return {'status': 'EXPIRED', 'intent_class': None,
                    'payload': None, 'event_obs': None, 'last_obs': obs}
        p['followups'].append(obs)
        n = len(p['followups'])
        base = {'payload': p['payload'], 'event_obs': p['event'], 'last_obs': obs}
        if n < config.INTENT_CONFIRMATION_BARS_MIN:
            return {'status': 'PENDING', 'intent_class': None, **base}
        ic = classify_intent(side, p['event'], p['followups'])
        if is_directional_demand(ic):
            self._pending.pop(key, None)
            return {'status': 'CONFIRMED', 'intent_class': ic, **base}
        if n >= config.INTENT_CONFIRMATION_BARS_MAX:
            self._pending.pop(key, None)
            return {'status': 'REJECTED', 'intent_class': ic, **base}
        return {'status': 'PENDING', 'intent_class': ic, **base}

    def drop(self, symbol: str, side: str, strike: float) -> None:
        self._pending.pop((symbol, side, float(strike)), None)

    def reset(self, symbol: str = None) -> None:
        if symbol is None:
            self._pending.clear()
        else:
            for k in [k for k in self._pending if k[0] == symbol]:
                self._pending.pop(k, None)

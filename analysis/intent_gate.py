"""
Deferred Gold-entry orchestrator (P2 live-wiring).

Bridges the detector's fired signals to the directional-intent validator so a Gold
candidate is not alerted on its event bar; instead it registers PENDING and is
promoted only once the next 1-3 completed bars confirm directional demand (§4/§6/§8),
with an opposite-side veto (§9) applied at confirmation.

Only used when GOLD_ONLY_PRODUCTION_MODE and INTENT_VALIDATION_ENABLED are both on;
main.py's existing immediate path is untouched otherwise. `obs_fn(side, strike)` is a
caller-supplied callback returning the current observation dict for a contract:
    {'mark', 'iv', 'spot', 'call_leadership', 'put_leadership'}.
"""
import logging

import config
from analysis import gold_mode
from analysis.intent_validation import (
    IntentValidator, opposite_side_veto, is_directional_demand,
)

logger = logging.getLogger(__name__)


def _is_exceptional(sig) -> bool:
    """#6 — an exceptional COMPLETED-bar event may fire immediately (no 1–3 bar wait):
    0DTE premium reprices fast, so a proven institutional-scale print shouldn't sit
    pending. Requires a completed (not partial) bar so we never fire on partial data."""
    if not config.ACTIVATION_FASTPATH_ENABLED:
        return False
    if sig.get('bar_status') not in ('COMPLETED', 'REVISED'):
        return False
    sym = sig.get('symbol')
    vol_min = config.ACTIVATION_EXCEPTIONAL_1M.get(sym, config.ACTIVATION_EXCEPTIONAL_1M['default'])
    tv = int(sig.get('trigger_volume') or 0)
    pn = float(sig.get('premium_notional') or 0.0)
    return tv >= vol_min or pn >= config.ACTIVATION_EXCEPTIONAL_NOTIONAL


class IntentGate:
    def __init__(self) -> None:
        self.v = IntentValidator()

    def reset(self, symbol: str = None) -> None:
        self.v.reset(symbol)

    def classify_new(self, symbol: str, signals: list, obs_fn) -> dict:
        """
        Classify fresh signals and route them:
          emit     — fire now (reversal subtypes are exempt from deferred intent)
          research — not a Gold-graded candidate (§19)
          deferred — Gold candidate registered PENDING (awaits confirmation)
        """
        emit, research, deferred = [], [], []
        for sig in signals:
            gold_mode.classify(sig)
            grade   = sig.get('gold_grade')
            subtype = sig.get('gold_subtype')
            if grade != 'GOLD' or subtype not in gold_mode._GOLD_SUBTYPES:
                research.append(sig)
            elif subtype == gold_mode.COUNTERTREND_REVERSAL:
                emit.append(sig)
            elif _is_exceptional(sig):
                # #6 fast-path — fire on the completed event bar, but still refuse to
                # fire into two-sided flow (opposite-side veto at the event obs).
                side   = sig.get('option_type')
                strike = float(sig.get('traded_strike') or 0)
                veto = opposite_side_veto(side, obs_fn(side, strike), 0.0)
                sig['opp_veto'] = veto
                if veto:
                    research.append(sig)
                    logger.info("INTENT fast-path vetoed: %s %s %s@%s", symbol, subtype, side, strike)
                else:
                    sig['intent_class'] = 'FAST_PATH_EXCEPTIONAL'
                    emit.append(sig)
                    logger.info("INTENT fast-path: %s %s %s@%s exceptional → fire now",
                                symbol, subtype, side, strike)
            else:
                side   = sig.get('option_type')
                strike = float(sig.get('traded_strike') or 0)
                self.v.register(symbol, side, strike, obs_fn(side, strike), payload=sig)
                deferred.append(sig)
                logger.info("INTENT defer: %s %s %s@%s pending confirmation",
                            symbol, subtype, side, strike)
        return {'emit': emit, 'research': research, 'deferred': deferred}

    def step(self, symbol: str, obs_fn) -> dict:
        """
        Advance every pending candidate for `symbol` by one observation. Returns:
          emit     — confirmed directional demand AND not opposite-side vetoed
          research — rejected/expired, or vetoed at confirmation
        """
        emit, research = [], []
        for side, strike in self.v.pending_items(symbol):
            obs = obs_fn(side, strike)
            res = self.v.observe(symbol, side, strike, obs)
            status = res['status']
            if status == 'PENDING':
                continue
            sig = res.get('payload')
            if sig is None:
                continue
            sig['intent_class'] = res.get('intent_class')
            if status == 'CONFIRMED' and is_directional_demand(res['intent_class']):
                ev = res.get('event_obs') or {}
                last = res.get('last_obs') or {}
                ev_mark = ev.get('mark') or 0.0
                prem_chg = ((last.get('mark') or 0.0) - ev_mark) / ev_mark if ev_mark > 0 else 0.0
                veto = opposite_side_veto(side, {**last, 'event_spot': ev.get('spot')}, prem_chg)
                sig['opp_veto'] = veto
                if veto:
                    logger.info("INTENT veto: %s %s@%s %s", symbol, side, strike, veto)
                    research.append(sig)
                else:
                    logger.info("INTENT confirmed: %s %s@%s %s", symbol, side, strike,
                                res['intent_class'])
                    emit.append(sig)
            else:
                logger.info("INTENT rejected: %s %s@%s %s (%s)", symbol, side, strike,
                            status, res.get('intent_class'))
                research.append(sig)
        return {'emit': emit, 'research': research}

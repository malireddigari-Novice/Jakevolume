"""
Event-time state registry (P-ET step 2).

Freezes market state at the moment a contract's volume first crosses the WATCH
threshold, so downstream eligibility uses the ATM / strike-distance / spot that were
true WHEN the flow occurred — not at bar-close after price has run away. This is the
fix for the TSLA-425P failure (ATM at the opening flow, deep ITM before the bar closed).

Lifecycle per contract, driven by EventRegistry.observe() each poll:
  1. WATCH cross   — r60 >= watch_vol: create EventState; snapshot spot + FREEZE the ATM
                     relationship + strike distance; set a TTL.
  2. THRESHOLD cross — r60 >= floor_60 OR r180 >= floor_180: stamp the decision-time
                     quotes/volumes/timestamp (once).
  3. STAYS ALIVE until ttl_expires_at even if the contract moves ITM/OTM.
  4. PRUNE on TTL (or reset() at a new session).

Feature-flagged by config.EVENT_TIME_ELIGIBILITY_ENABLED at the call site; this module
is pure state and has no effect until the detector consults it (P-ET step 3+).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class EventState:
    symbol: str
    strike: float
    option_type: str
    # ── frozen at WATCH cross ──
    event_start_time: datetime
    spot_at_event_start: float
    atm_strike_at_event_start: float
    strike_distance_at_event: float           # |contract_strike - event-time ATM| in dollars
    ttl_expires_at: datetime
    # ── stamped at THRESHOLD cross (once) ──
    crossed: bool = False
    threshold_cross_time: Optional[datetime] = None
    spot_at_threshold_cross: Optional[float] = None
    atm_strike_at_threshold_cross: Optional[float] = None
    bid_at_threshold: Optional[float] = None
    ask_at_threshold: Optional[float] = None
    last_at_threshold: Optional[float] = None
    r60_at_threshold: Optional[int] = None
    r180_at_threshold: Optional[int] = None
    observed_volume_at_decision: Optional[int] = None
    decision_timestamp: Optional[datetime] = None

    def strike_distance_strikes(self, increment: float) -> Optional[int]:
        """Event-time distance expressed in strikes, given the chain's strike increment."""
        if not increment or increment <= 0:
            return None
        return round(self.strike_distance_at_event / increment)


class EventRegistry:
    """Per-contract event-time state, keyed by (symbol, strike, option_type)."""

    def __init__(self) -> None:
        self._states: dict = {}

    def observe(self, symbol: str, strike: float, option_type: str, *,
                now: datetime, spot: float, atm_strike: float,
                r60: int, r180: int, floor_60: int, floor_180: int,
                bid=None, ask=None, last=None,
                watch_vol: int, ttl_min: int) -> Optional[EventState]:
        """
        Advance one contract's event-time lifecycle by one observation. Returns the
        live EventState (created/updated), or None if below watch or expired.
        """
        key = (symbol, float(strike), option_type)
        st = self._states.get(key)

        # prune on TTL
        if st is not None and now > st.ttl_expires_at:
            self._states.pop(key, None)
            st = None

        # WATCH cross → create + freeze event-time context
        if st is None:
            if (r60 or 0) < watch_vol:
                return None
            st = EventState(
                symbol=symbol, strike=float(strike), option_type=option_type,
                event_start_time=now,
                spot_at_event_start=spot,
                atm_strike_at_event_start=atm_strike,
                strike_distance_at_event=abs(float(strike) - atm_strike),
                ttl_expires_at=now + timedelta(minutes=ttl_min),
            )
            self._states[key] = st

        # THRESHOLD cross → stamp decision state once
        if not st.crossed and ((r60 or 0) >= floor_60 or (r180 or 0) >= floor_180):
            st.crossed = True
            st.threshold_cross_time = now
            st.spot_at_threshold_cross = spot
            st.atm_strike_at_threshold_cross = atm_strike
            st.bid_at_threshold, st.ask_at_threshold, st.last_at_threshold = bid, ask, last
            st.r60_at_threshold, st.r180_at_threshold = r60, r180
            st.observed_volume_at_decision = r60
            st.decision_timestamp = now
        return st

    def get(self, symbol: str, strike: float, option_type: str) -> Optional[EventState]:
        return self._states.get((symbol, float(strike), option_type))

    def prune(self, now: datetime) -> int:
        expired = [k for k, s in self._states.items() if now > s.ttl_expires_at]
        for k in expired:
            self._states.pop(k, None)
        return len(expired)

    def reset(self, symbol: str = None) -> None:
        if symbol is None:
            self._states.clear()
        else:
            for k in [k for k in self._states if k[0] == symbol]:
                self._states.pop(k, None)

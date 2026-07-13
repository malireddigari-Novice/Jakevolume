"""
Flow Leadership Reversal Engine (V1) — analysis/flow_reversal.py.

The original OI signal starts the trade but does not lock the ending. Once a position
is open this engine watches the OPPOSITE side of the flow: when it produces a
concentrated volume event (a burst out of a quiet background, contract still near its
low) while the SAME side fades, the bearish/bullish story has likely flipped — exit
and emit a reversal alert.

V1 (spec §19): close the current (paper) position + alert + record the hypothetical
opposite entry. It does NOT auto-open the opposite trade (gated by
config.FLOW_REVERSAL_AUTO_FLIP, default off) until tested.

Concentrated volume event (spec §3, on completed 1-min option-volume bars, oldest→newest):
    EventVol   = sum of the last 5 bars            EventAvg = EventVol / 5
    PreEventVol= avg of the ~15 bars before them
    BurstRatio = EventAvg / max(PreEventVol, 1)
    EventShare = EventVol / max(sum(last 20), 1)
    ActiveEventBars = bars in the last 5 with vol >= 2*PreEventVol
    PersistentBackgroundFlow (§5): >=10 of the last 20 bars >= 0.5*Peak20 AND share<0.40 → reject
    ValidVolumeEvent = Burst>=3 AND Share>=0.40 AND Active>=2 AND not persistent AND near-low
"""
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Position states (spec §2)
NO_POSITION = 'NO_POSITION'
ACTIVE = 'ACTIVE'                     # CALL_ACTIVE / PUT_ACTIVE collapsed (side tracked separately)
REVERSAL_WATCH = 'REVERSAL_WATCH'
REVERSAL_CONFIRMED = 'REVERSAL_CONFIRMED'


def volume_event(hist: list) -> Optional[dict]:
    """Concentrated-volume-event metrics over 1-min option volumes (oldest→newest)."""
    h = [int(x) for x in hist]
    if len(h) < 6:
        return None
    last20   = h[-20:]
    event    = last20[-5:]
    pre_bars = last20[:-5]                       # up to 15 prior bars
    pre      = (sum(pre_bars) / len(pre_bars)) if pre_bars else 0.0
    event_vol = sum(event)
    burst = (event_vol / 5.0) / max(pre, 1.0)
    total20 = sum(last20)
    share = event_vol / max(total20, 1.0)
    active = (sum(1 for b in event if b >= 2 * pre) if pre > 0
              else sum(1 for b in event if b > 0))
    peak20 = max(last20) if last20 else 0
    active_bg = sum(1 for b in last20 if b >= 0.5 * peak20) if peak20 > 0 else 0
    persistent_bg = (active_bg >= 10 and share < config.REVERSAL_EVENT_SHARE)
    valid = (burst >= config.REVERSAL_BURST_RATIO
             and share >= config.REVERSAL_EVENT_SHARE
             and active >= config.REVERSAL_ACTIVE_BARS
             and not persistent_bg)
    return dict(pre=round(pre, 1), event_vol=event_vol, burst=round(burst, 2),
                share=round(share, 3), active=active, peak20=peak20,
                active_bg=active_bg, persistent_bg=persistent_bg, valid=valid)


def _event_score(ev: Optional[dict]) -> float:
    """VolumeEventScore (spec §9) — positive only when ValidVolumeEvent."""
    if not ev or not ev['valid']:
        return 0.0
    b, s = ev['burst'], ev['share']
    if b >= 5.0 and s >= 0.60: return 1.00
    if b >= 4.0 and s >= 0.50: return 0.85
    if b >= 3.0 and s >= 0.40: return 0.75
    return 0.0


def _near_low_score(dist: Optional[float]) -> float:
    if dist is None: return 0.0
    if dist <= 1.25: return 1.00
    if dist <= 1.50: return 0.85
    if dist <= 1.75: return 0.70
    return 0.0


def _concentration(ev: Optional[dict]) -> float:
    if not ev or not ev['valid']:
        return 0.0
    return round(min(1.0, (ev['burst'] / 5.0) * 0.6 + ev['share'] * 0.4
                     + (0.2 if ev['active'] >= 3 else 0.0)), 3)


def _leadership(events: list) -> dict:
    """
    Side leadership score (spec §8/§9) from a side's watched contracts.
    `events`: list of {'strike', 'ev', 'low_dist'} for ATM/ITM/OTM of that side.
    """
    valids = [e for e in events if e['ev'] and e['ev']['valid']
              and (e['low_dist'] is None or e['low_dist'] <= config.REVERSAL_NEAR_LOW_MAX)]
    if not valids:
        return dict(score=0.0, ves=0.0, nls=0.0, breadth=0.0, best=None, n_valid=0)
    best = max(valids, key=lambda e: _event_score(e['ev']))
    ves = _event_score(best['ev'])
    conc = _concentration(best['ev'])
    nls = max(_near_low_score(e['low_dist']) for e in valids)
    breadth = 1.00 if len(valids) >= 2 else 0.70
    score = 0.40 * ves + 0.25 * conc + 0.20 * nls + 0.15 * breadth
    return dict(score=round(score, 3), ves=ves, nls=nls, breadth=breadth,
                best=best, n_valid=len(valids))


class FlowReversalEngine:
    """Per-symbol stateful reversal detector. One open position per symbol assumed."""

    def __init__(self) -> None:
        self._state: dict = {}

    def reset(self, symbol: str) -> None:
        self._state.pop(symbol, None)

    def evaluate(self, symbol: str, pos_type: str, same_events: list,
                 opp_events: list, now, price_confirmed=None) -> dict:
        """
        pos_type    : 'CALL' or 'PUT' (the open position's option side)
        same_events : watched contracts on the position side  [{strike, ev, low_dist, mark}]
        opp_events  : watched contracts on the opposite side
        price_confirmed : caller's price-confirmation verdict (VWAP loss for a call
                          position / reclaim for a put). None when the layer is off.
        Returns a dict with state, reversal_confirmed flag, leadership scores, and log fields.
        """
        st = self._state.setdefault(symbol, dict(
            last_same_t=now, same_peak=0.0, last_opp_t=None, opp_streak=0,
            opp_mark_ref=None, state=ACTIVE))

        same = _leadership(same_events)
        opp  = _leadership(opp_events)

        same_now_vol = max((e['ev']['event_vol'] for e in same_events if e['ev']), default=0)
        opp_best     = opp['best']
        opp_valid    = opp['n_valid'] > 0

        # ── Same-side flow health (§6) ─────────────────────────────────────────
        if same['n_valid'] > 0:
            st['last_same_t'] = now
            st['same_peak'] = max(st['same_peak'],
                                  max(e['ev']['event_vol'] for e in same_events
                                      if e['ev'] and e['ev']['valid']))
        if st['same_peak'] <= 0:
            st['same_peak'] = max(same_now_vol, 1)
        same_age = (now - st['last_same_t']).total_seconds() / 60.0
        same_fading = (same_age > config.REVERSAL_FADE_WINDOW_MIN
                       and same_now_vol <= config.REVERSAL_FADE_RATIO * st['same_peak'])

        # ── Opposite-side validation (§7) + streak for the "two evaluations" rule ─
        if opp_valid:
            st['last_opp_t'] = now
            st['opp_streak'] += 1
        else:
            st['opp_streak'] = 0

        # ── Premium confirmation (V2): the taking-control (opp) side's premium must
        # EXPAND during the takeover. Track the streak-low opp mark; require the
        # current opp mark to have risen off it by REVERSAL_PREMIUM_EXPANSION_PCT.
        # This blocks flips into stagnant/decaying far-OTM pennies (why the engine
        # was disabled). Same-side stagnation is already captured by `same_fading`.
        opp_mark = opp_best.get('mark') if opp_best else None
        if st['opp_streak'] == 0:
            st['opp_mark_ref'] = None                       # streak broke → forget reference
        elif opp_mark and opp_mark > 0:
            st['opp_mark_ref'] = (opp_mark if st.get('opp_mark_ref') is None
                                  else min(st['opp_mark_ref'], opp_mark))
        premium_confirmed = True
        if config.REVERSAL_PREMIUM_CONFIRM_ENABLED:
            ref = st.get('opp_mark_ref')
            premium_confirmed = bool(opp_mark and ref
                                     and opp_mark >= ref * (1 + config.REVERSAL_PREMIUM_EXPANSION_PCT))

        # ── Price confirmation (V2): the underlying must validate the shift (caller
        # passes VWAP-loss for a call position / VWAP-reclaim for a put position). ──
        price_ok = True
        if config.REVERSAL_PRICE_CONFIRM_ENABLED:
            price_ok = bool(price_confirmed)

        # transition window (§11): opp & same events within REVERSAL_WINDOW_MIN
        window_ok = (st['last_opp_t'] is not None and st['last_same_t'] is not None
                     and abs((st['last_opp_t'] - st['last_same_t']).total_seconds())
                     <= config.REVERSAL_WINDOW_MIN * 60)

        # no new stronger same-side event than the opposite (§19.4)
        opp_vol = opp_best['ev']['event_vol'] if opp_best else 0
        no_stronger_same = same_now_vol < opp_vol or same['n_valid'] == 0

        # leadership change (§10)
        diff = round(opp['score'] - same['score'], 3)
        leadership_change = (opp['score'] >= config.REVERSAL_LEADERSHIP_MIN
                             and diff >= config.REVERSAL_LEADERSHIP_DIFF and same_fading)

        # confirmation (§13/§19.6): two evaluations, OR multi-strike, OR one dominant event
        dominant = bool(opp_best and opp_best['ev']['burst'] >= config.REVERSAL_DOMINANT_BURST
                        and opp_best['ev']['share'] >= config.REVERSAL_DOMINANT_SHARE)
        confirmation = (st['opp_streak'] >= 2 or opp['n_valid'] >= 2 or dominant)

        watch = opp_valid and same_fading and not (opp_best and opp_best['ev']['persistent_bg'])
        confirmed = bool(watch and leadership_change and window_ok
                         and confirmation and no_stronger_same
                         and premium_confirmed and price_ok)

        st['state'] = (REVERSAL_CONFIRMED if confirmed else
                       REVERSAL_WATCH if watch else ACTIVE)

        return {
            'state': st['state'],
            'reversal_confirmed': confirmed,
            'reversal_watch': watch,
            'opp_type': 'PUT' if pos_type == 'CALL' else 'CALL',
            'same_leadership': same['score'], 'opp_leadership': opp['score'],
            'leadership_diff': diff, 'same_fading': same_fading,
            'opp_valid': opp_valid, 'opp_streak': st['opp_streak'],
            'window_ok': window_ok, 'confirmation': confirmation, 'dominant': dominant,
            'opp_best': opp_best, 'same_now_vol': same_now_vol, 'opp_vol': opp_vol,
            'premium_confirmed': premium_confirmed, 'price_confirmed': bool(price_confirmed),
            'opp_mark_ref': st.get('opp_mark_ref'),
        }

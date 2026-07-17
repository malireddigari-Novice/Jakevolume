"""
Stateful intraday signal detector — Simplified V1 entry engine.

Goal (Mag-7 only): every minute, decide whether to alert a CALL or a PUT.

Signal semantics
-----------------
  SUPPORT    level (S1/S2/S3) → BULLISH / "Call-side bias"  — confirmed by CALL volume
  RESISTANCE level (R1/R2/R3) → BEARISH / "Put-side bias"   — confirmed by PUT  volume

A CALL/PUT alert fires when ALL hold (§17/§18):
  • spot is NEAR a same-side level                        (§4 proximity, binary)
  • correct side is being watched                         (§5)
  • a valid volume signal exists  (ValidVolumeSignal = SingleBar OR Cluster OR StairStep)
  • the contract is cheap / not chased                    (§12 contract-low)
  • the contract is not historically rich                 (§13 historical percentile)
  • there is no short-cover risk                          (§14)
  • this symbol has not already alerted this direction    (§19 — 1 call + 1 put/day)

Valid volume signal (any one — the kind is NOT surfaced to Discord, §1):
  §9  Extreme single print : vol >= MinSingle AND ratio >= 8.0 AND low_dist <= 1.75
  §10 Volume cluster       : WindowVol5 >= MinCluster AND WindowRatio5 >= 3.0
                             AND ActiveBars5 >= 3 AND low_dist <= 1.75
  §11 Stair-step accum.    : ExcitationScore >= 0.70 AND WindowRatio5 >= 2.5
                             AND ActiveBars5 >= 3 AND low_dist <= 2.0

Opening range (§15): first 15 min are NOT blocked but need stronger evidence —
MinSingle × 1.5, cluster WindowRatio5 >= 4.0, stair-step ExcitationScore >= 0.80.

Removed for V1 (§1): dynamic S/R flipping, spread filter, target-room filter,
confidence tiers, WATCH/upgrade alerts, volume-shape labels in Discord.
"""
import logging
import statistics
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Optional

import config
from analysis.flow_reversal import volume_event
from analysis.volume_analytics import compute_leadership_scores
from analysis.trend import IntradayTrend
from analysis.event_state import EventRegistry
from analysis.rolling_volume import RollingVolume
from analysis.opening_scan import scan_opening, opening_story, opening_side_confirmed
from analysis import breakout as _breakout
from analysis.route_b import route_b_qualifies
from analysis import chain_leadership as _chain_lead
from analysis.gold_mode import contract_low_region
from analysis import premium_discovery as _premium_discovery
from analysis import alert_taxonomy as _taxonomy
from analysis.paper_fill import executable_fill as _executable_fill
from data.alpaca_client import occ_symbol
from data.market_utils import CST

logger = logging.getLogger(__name__)

_FiredKey = tuple[str, str]          # (symbol, signal_type)
_OptKey   = tuple[str, float, str]   # (symbol, strike, opt_type)


# ── Pure helper functions ─────────────────────────────────────────────────────

def _spike_ratio(history: list[int], delta: int) -> float:
    """1-bar ratio: delta / max(rolling_avg, 10). Returns 0.0 with no prior history."""
    prior = history[:-1] if len(history) > 1 else []
    if not prior:
        return 0.0
    baseline = max(sum(prior) / len(prior), 10)   # spec: max(AvgVol, 10)
    return round(delta / baseline, 2)


def _window_ratio(history: list[int]) -> tuple[int, float]:
    """
    3-bar window sum vs prior rolling windows (used by check_opposite_side).
    Returns (window_vol, window_spike_ratio). Needs >=4 bars; else (sum, 0.0).
    """
    if len(history) < 4:
        return sum(history), 0.0
    window_vol    = sum(history[-3:])
    prior_windows = [sum(history[i:i+3]) for i in range(len(history) - 3)]
    prior_avg     = sum(prior_windows) / len(prior_windows) if prior_windows else 0
    ratio         = window_vol / max(prior_avg, 30)
    return window_vol, round(ratio, 2)


def _avg_prior(history: list[int], exclude_last: int, lookback: int) -> float:
    """
    Average of up to `lookback` bars ending `exclude_last` bars before the end.
    exclude_last=1 → bars just before the current one (single print);
    exclude_last=N → bars before the trailing N-bar window (cluster baseline).
    Returns 0.0 when no prior bars are available.
    """
    end = len(history) - exclude_last
    if end <= 0:
        return 0.0
    seg = history[max(0, end - lookback):end]
    return sum(seg) / len(seg) if seg else 0.0


def _single_print(history: list[int], delta: int, min_vol: int) -> tuple[bool, float]:
    """
    §9 raw single print (contract-low filter applied by caller):
      delta >= min_vol  AND  delta / max(AvgPrior10, 10) >= OPT_SINGLE_PRINT_RATIO.
    Returns (valid, ratio).
    """
    base  = max(_avg_prior(history, 1, config.OPT_PRIOR_LOOKBACK), 10.0)
    ratio = round(delta / base, 2)
    valid = delta >= min_vol and ratio >= config.OPT_SINGLE_PRINT_RATIO
    return valid, ratio


def _cluster_metrics(history: list[int]) -> dict:
    """
    Rolling N-bar window metrics (N = OPT_CLUSTER_WINDOW, default 5).

    Returns a dict with:
      vol        : sum of the last N bar deltas (WindowVol5)
      ratio      : WindowVol5 / (N * base_unit)  (WindowRatio5)
      active     : bars with per-bar ratio >= OPT_CLUSTER_ACTIVE_RATIO
      burst      : bars with per-bar ratio >= OPT_CLUSTER_BURST_RATIO
      base_unit  : max(AvgPrior10 before the window, 10)
      window     : the last N bar deltas (for the stair-step excitation)
    A short history (< N bars) yields zeroed metrics so nothing fires early.
    """
    n = config.OPT_CLUSTER_WINDOW
    window = list(history[-n:])
    if len(window) < n:
        return {'vol': sum(window), 'ratio': 0.0, 'active': 0, 'burst': 0,
                'base_unit': 10.0, 'window': window}
    base_unit  = max(_avg_prior(history, n, config.OPT_PRIOR_LOOKBACK), 10.0)
    window_vol = sum(window)
    ratio      = round(window_vol / (n * base_unit), 2)
    active = sum(1 for b in window if b / base_unit >= config.OPT_CLUSTER_ACTIVE_RATIO)
    burst  = sum(1 for b in window if b / base_unit >= config.OPT_CLUSTER_BURST_RATIO)
    return {'vol': window_vol, 'ratio': ratio, 'active': active, 'burst': burst,
            'base_unit': base_unit, 'window': window}


def _excitation(window: list[int], base_unit: float) -> float:
    """
    §11 ExcitationScore from the most-recent-first per-bar ratios:
      ExcitationRaw   = Σ STAIRSTEP_WEIGHTS[i] * VolumeRatio[t-i]
      ExcitationScore = min(ExcitationRaw, 10) / 10
    where VolumeRatio[t-i] = window[-1-i] / base_unit. 0.0 with no window.
    """
    if not window or base_unit <= 0:
        return 0.0
    weights = config.STAIRSTEP_WEIGHTS
    raw = 0.0
    for i, w in enumerate(weights):
        if i >= len(window):
            break
        raw += w * (window[-1 - i] / base_unit)
    return round(min(raw, 10.0) / 10.0, 4)


def _timing_score(atm_hist: list[int], itm_hist: list[int]) -> float:
    """Informational only — feeds the stored cluster_strength field."""
    n = min(3, len(atm_hist), len(itm_hist))
    if n == 0:
        return 0.0
    atm_3, itm_3 = atm_hist[-n:], itm_hist[-n:]
    if max(atm_3) == 0 or max(itm_3) == 0:
        return 0.0
    return 1.00 if atm_3.index(max(atm_3)) == itm_3.index(max(itm_3)) else 0.70


def _pc_conviction(signal_type: str, pc_ratio: Optional[float]) -> str:
    """Informational P/C label (no longer a gate)."""
    if pc_ratio is None:
        return 'NEUTRAL'
    if signal_type == 'BULLISH':
        if pc_ratio < config.PC_BULL_CUTOFF:
            return 'WITH_BIAS'
        if pc_ratio > config.PC_BEAR_CUTOFF:
            return 'AGAINST_BIAS'
    else:
        if pc_ratio > config.PC_BEAR_CUTOFF:
            return 'WITH_BIAS'
        if pc_ratio < config.PC_BULL_CUTOFF:
            return 'AGAINST_BIAS'
    return 'NEUTRAL'


def _spread_pct(data: dict) -> Optional[float]:
    """(ask − bid) / mid — informational only in V1 (no longer a gate)."""
    bid, ask = data.get('bid'), data.get('ask')
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


def _opposing_strikes(levels: list, spot: float, position_only: bool) -> tuple[list, list]:
    """
    Split level strikes into (above_spot, below_spot). Used to derive exit
    targets for the (separate) exit state machine, not for entry.
    """
    above, below = [], []
    for l in levels:
        s = float(l['strike'])
        if position_only:
            (above if s > spot else below).append(s)
        elif l['level_type'] == 'RESISTANCE' and s > spot:
            above.append(s)
        elif l['level_type'] == 'SUPPORT' and s < spot:
            below.append(s)
    return above, below


def compute_exit_targets(
    signal_type: str,
    spot: float,
    levels: list,
    position_only: bool = False,
    origin_level: Optional[float] = None,
) -> tuple[Optional[float], Optional[float]]:
    """
    Exit-target ladder — take the NEXT levels the trade moves into, skipping a level
    only when it is too close to the entry (not always skipping the nearest).

    The ladder is ALL level strikes on the move side, not only the opposing side: a
    CALL climbs up through every level above the entry, a PUT falls through every level
    below it. So a call entered at S3 targets S2, then S1, then R1, R2, R3 in order;
    a put entered at R3 targets R2, R1, S1, S2, S3.

    Exit1 / Exit2 = the first two ladder levels that clear EXIT_MIN_ROOM_PCT of room from
    the entry. A level within that distance is skipped (it would sell the first half too
    soon); a level with room is KEPT (we no longer blindly skip the nearest). Examples:

      Call entered ~S3:  S2 too close → Exit1 = S1, Exit2 = R1.
      Call entered ~S2:  S1 too close → Exit1 = R1, Exit2 = R2.
      Call entered ~S1:  R1 too close → Exit1 = R2, Exit2 = R3.
      (mirror for puts entered at R3 / R2 / R1)
      If the next level has room, it is used as-is (no skip).

    Fallbacks: if every level is too close, keep the raw nearest two; only one level on
    the move side → Exit2 is None. `position_only` is retained for call-site compatibility
    (the ladder always spans all levels regardless).

    Target integrity (§10/§20): the originating level can NEVER be a target. A CALL's
    targets must be strictly above BOTH the entry spot AND the origin level; a PUT's
    strictly below both. `origin_level` is the entry level price; when given it moves
    the ladder floor/ceiling out to max(spot, origin) / min(spot, origin) so the origin
    strike (which often sits just past spot) can no longer be selected as Exit1.

    Returns (exit1, exit2) as underlying price levels, either may be None.
    """
    if signal_type == 'BULLISH':
        floor_px = max(spot, origin_level) if origin_level is not None else spot
        ladder = sorted(float(l['strike']) for l in levels if float(l['strike']) > floor_px)
        room   = lambda lv: (lv - spot) / spot if spot > 0 else 0.0
    else:
        ceil_px = min(spot, origin_level) if origin_level is not None else spot
        ladder = sorted((float(l['strike']) for l in levels if float(l['strike']) < ceil_px),
                        reverse=True)
        room   = lambda lv: (spot - lv) / spot if spot > 0 else 0.0

    if not ladder:
        return None, None
    spaced = [lv for lv in ladder if room(lv) >= config.EXIT_MIN_ROOM_PCT]
    chosen = spaced if spaced else ladder            # all too close → keep the raw nearest
    return chosen[0], (chosen[1] if len(chosen) > 1 else None)


# ── Detector ──────────────────────────────────────────────────────────────────

class SignalDetector:

    def __init__(self) -> None:
        # Retain (close to) the full session per contract so the VolumeStickoutScore
        # can compute its 20/60-bar baselines, session percentile and 5-min windows.
        self._hist_maxlen = max(config.SESSION_BARS,
                                config.OPT_CLUSTER_WINDOW + config.OPT_PRIOR_LOOKBACK)
        # One alert per direction per ticker per day.
        self._fired_today:  dict[_FiredKey, bool] = {}
        self._prev_opt_vol: dict[_OptKey, int]    = {}
        self._opt_vol_hist: dict[_OptKey, deque]  = defaultdict(
            lambda: deque(maxlen=self._hist_maxlen)
        )
        self._opt_mark_low: dict[_OptKey, float]      = {}
        self._opt_last_bar: dict[_OptKey, datetime]   = {}
        # §13 multi-day (low, high) per OCC, fetched once per contract per day.
        self._opt_hist_range: dict[str, Optional[tuple[float, float]]] = {}
        self._hist_range_fn = None
        # Fallback when no live multi-day history exists (Schwab serves no option
        # price-history): the contract's previous-session (low, high) from the DB.
        self._prev_range_fn = None
        # §13b Premium Discovery Score: the contract's premium/volume history
        # (list of {low,high,close,volume}) per OCC, fetched once per contract/day.
        self._opt_premium_hist: dict[str, list] = {}
        self._premium_hist_fn = None
        # §7-8 completed 1-min bar volume provider + per-contract/minute cache,
        # plus the pending-volume-confirmation set (near-miss partial-bar candidates).
        self._completed_bar_fn = None
        self._completed_bar_cache: dict = {}
        self._pending_candidates: dict = {}
        # §8-14 intraday trend tracker + countertrend watch set (reset daily).
        self._trend = IntradayTrend()
        self._countertrend_watch: dict = {}
        # §14 prior major volume events per contract key → [(volume, price), …].
        self._major_events: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self._history_date: Optional[date] = None
        # §73 per-poll candidate-evaluation log (every level, blocked or passed) — the
        # caller persists `last_candidates` to signal_candidates after each check().
        self.last_candidates: list[dict] = []
        self.last_leadership_shadow: list[dict] = []
        self._leadership_shadow_fired: dict = {}
        # P-ET event-time capture (only fed when EVENT_TIME_ELIGIBILITY_ENABLED).
        self._event_reg = EventRegistry()
        self._rvol: dict[_OptKey, RollingVolume] = {}
        self.last_opening_candidates: list[dict] = []   # event-time opening scan (research)

    # ── Public API ────────────────────────────────────────────────────────────

    def check(
        self,
        symbol: str,
        bars: list[dict],
        levels: list,
        option_quotes: dict | None = None,
        expiry: Optional[date] = None,
        pc_ratio: Optional[float] = None,
        opening_range: bool = False,
        hist_range_fn=None,
        fired_today_fn=None,
        prev_range_fn=None,
        completed_bar_fn=None,
        premium_hist_fn=None,
    ) -> list[dict]:
        """
        Run the V1 entry pipeline for the latest 1-min bar; return [] or [signal].

        opening_range : True during the first OPENING_RANGE_MINUTES — not blocked,
                        but volume thresholds are raised (§15).
        hist_range_fn : callable(occ) -> (low, high) | None for the §13 gate.
        fired_today_fn: callable(symbol, day) -> {signal_type: [confidence]} so the
                        one-call/one-put-per-day dedup survives restarts/instances.
        prev_range_fn : callable(symbol, strike, opt_type, before_date) -> (low, high) | None.
                        §13 historical range: the contract's FULL stored-history (low, high)
                        from the DB, used when hist_range_fn yields nothing (no live option
                        price-history). Backs the "at/near historical low" requirement.
        premium_hist_fn: callable(symbol, strike, opt_type, before_date) -> [ {low,high,
                        close,volume}, … ] for the §13b Premium Discovery Score — the
                        contract's premium/volume distribution over prior sessions. None
                        (or empty) → PDS is skipped/unknown (non-blocking unless strict).
        """
        self.last_candidates = []          # §73 — reset per poll
        self.last_leadership_shadow = []   # V2 shadow leadership signals — main reads post-check()
        if not bars:
            return []

        current     = bars[-1]
        close_price = current['close']
        today       = current['bar_time'].date()
        bar_time    = current['bar_time']
        self._hist_range_fn = hist_range_fn
        self._prev_range_fn = prev_range_fn
        self._completed_bar_fn = completed_bar_fn
        self._premium_hist_fn = premium_hist_fn

        # Keep 1DTE expiry handling (target-strike pricing); roles stay frozen.
        next_day_mode = (config.NEXT_DAY_MODE_ENABLED and
                         expiry is not None and expiry > today)

        # Reset intraday state on a new trading day
        if self._history_date != today:
            self._history_date    = today
            self._fired_today     = {}
            self._leadership_shadow_fired = {}   # V2 shadow one-per-direction-per-day
            self._prev_opt_vol    = {}
            self._opt_vol_hist    = defaultdict(lambda: deque(maxlen=self._hist_maxlen))
            self._opt_mark_low    = {}
            self._opt_last_bar    = {}
            self._opt_hist_range  = {}
            self._opt_premium_hist = {}
            self._major_events    = defaultdict(list)
            self._completed_bar_cache = {}
            self._pending_candidates  = {}
            self._trend.reset()
            self._countertrend_watch  = {}
            self._event_reg.reset()
            self._rvol = {}

        # Durable dedup: fold in directions already fired today (DB).
        if fired_today_fn is not None:
            try:
                db_fired = fired_today_fn(symbol, today) or {}
            except Exception as exc:
                logger.warning("fired_today_fn failed for %s: %s", symbol, exc)
                db_fired = {}
            for st in db_fired:
                self._fired_today[(symbol, st)] = True

        if not option_quotes:
            return []

        # ── Step 1: deltas + rolling histories (a re-entry after a gap restarts) ─
        gap_limit = config.POLL_INTERVAL_SECONDS * 1.5
        opt_data_map: dict[tuple[float, str], dict] = {}
        vol_deltas:   dict[tuple[float, str], int]  = {}

        # P-ET: current ATM per side (frozen into each contract's EventState on watch
        # cross). Entire block is a no-op unless EVENT_TIME_ELIGIBILITY_ENABLED.
        _et_on = config.EVENT_TIME_ELIGIBILITY_ENABLED
        _atm_call = _atm_put = None
        if _et_on:
            _cs = [s for (s, ot) in option_quotes if ot == 'CALL']
            _ps = [s for (s, ot) in option_quotes if ot == 'PUT']
            _atm_call = min(_cs, key=lambda s: abs(s - close_price)) if _cs else None
            _atm_put  = min(_ps, key=lambda s: abs(s - close_price)) if _ps else None

        for (s, ot), data in option_quotes.items():
            opt_key: _OptKey = (symbol, s, ot)
            cur_vol  = int(data.get('volume', 0) or 0)
            last_bar = self._opt_last_bar.get(opt_key)
            discontinuous = (last_bar is not None and
                             (bar_time - last_bar).total_seconds() > gap_limit)
            if last_bar is None or discontinuous:
                delta = 0
                if discontinuous:
                    self._opt_vol_hist[opt_key].clear()
            else:
                prev_vol = self._prev_opt_vol.get(opt_key, cur_vol)
                delta    = max(0, cur_vol - prev_vol)
            self._prev_opt_vol[opt_key] = cur_vol
            self._opt_last_bar[opt_key] = bar_time
            self._opt_vol_hist[opt_key].append(delta)

            # P-ET: feed rolling volume + event-time registry for every subscribed
            # contract (freezes ATM/spot/quotes at watch/threshold cross). Gated.
            if _et_on:
                rv = self._rvol.get(opt_key)
                if rv is None:
                    rv = RollingVolume()
                    self._rvol[opt_key] = rv
                rv.observe_delta(delta)
                _atm = _atm_call if ot == 'CALL' else _atm_put
                if _atm is not None:
                    self._event_reg.observe(
                        symbol, s, ot, now=bar_time, spot=close_price, atm_strike=_atm,
                        r60=rv.r60(), r180=rv.r180(),
                        floor_60=config.PEAK_1M_VOLUME_MIN, floor_180=config.VOLUME_3M_MIN,
                        bid=data.get('bid'), ask=data.get('ask'), last=data.get('mark'),
                        watch_vol=config.OPENING_EVENT_WATCH_VOLUME,
                        ttl_min=config.OPENING_EVENT_CONTRACT_TTL_MIN)

            mark = data.get('mark')
            if mark is not None and mark > 0:
                prev_low = self._opt_mark_low.get(opt_key)
                self._opt_mark_low[opt_key] = mark if prev_low is None else min(prev_low, mark)

            opt_data_map[(s, ot)] = data
            vol_deltas[(s, ot)]   = delta

        # ── §8-14 Per-poll leadership + intraday trend update (computed once) ──
        leadership = (compute_leadership_scores(symbol, opt_data_map, self._opt_vol_hist,
                                                self._contract_low_dist)
                      if opt_data_map else None)
        self._trend.update(symbol, close_price, bar_time, leadership)

        # P-ET step 5: opening ATM±N event-time scan (research-only, gated). Surfaces
        # contracts that crossed the floor at event time within ATM±window strikes AT
        # THAT MOMENT — kept eligible even if spot ran away. Logged/recorded, not fired.
        self.last_opening_candidates = []
        if _et_on and opening_range:
            try:
                self.last_opening_candidates = scan_opening(
                    symbol, option_quotes, self._event_reg,
                    window_strikes=config.OPENING_STRIKE_WINDOW)
                for c in self.last_opening_candidates:
                    logger.info("OPENING-SCAN eligible (event-time): %s %s $%.2f  "
                                "dist=%s strikes  %s",
                                symbol, c['option_type'], c['strike'],
                                c['dist_strikes'], c['no_retro'])
            except Exception:
                logger.warning("%s: opening scan failed", symbol, exc_info=True)

        # ── Opening-range thresholds (§15) — raised, never suppressed ──────────
        single_mult     = config.OPENING_RANGE_VOL_MULT      if opening_range else 1.0
        cluster_ratio_min = (config.OPENING_RANGE_CLUSTER_RATIO if opening_range
                             else config.OPT_CLUSTER_WINDOW_RATIO)
        excitation_min  = (config.OPENING_RANGE_EXCITATION_MIN if opening_range
                           else config.STAIRSTEP_EXCITATION_MIN)
        base_single_vol = config.OPT_MIN_SINGLE_PRINT_VOL.get(
            symbol, config.OPT_MIN_SINGLE_PRINT_VOL['default'])
        min_single_vol  = int(round(base_single_vol * single_mult))
        min_cluster_vol = config.OPT_MIN_CLUSTER_WINDOW_VOL.get(
            symbol, config.OPT_MIN_CLUSTER_WINDOW_VOL['default'])
        near_thr = (config.NEAR_LEVEL_DIST_VOLATILE if symbol in config.VOLATILE_SYMBOLS
                    else config.NEAR_LEVEL_DIST_DEFAULT)

        # Mandatory tightened production volume floors — stricter in the opening 15m.
        # Applied to BOTH the primary-level path (_eval_volume) and the chain-led path.
        peak1m_floor = (config.OPENING_PEAK_1M_VOLUME_MIN if opening_range
                        else config.PEAK_1M_VOLUME_MIN)
        vol3m_floor  = (config.OPENING_VOLUME_3M_MIN if opening_range
                        else config.VOLUME_3M_MIN)

        candidates: list[dict] = []

        for level in levels:
            strike       = float(level['strike'])
            rank         = int(level.get('rank', 1))
            level_type   = level['level_type']                       # frozen (no flip)
            label        = ('R' if level_type == 'RESISTANCE' else 'S') + str(rank)

            # ── §4 Proximity (binary) ─────────────────────────────────────────
            dist = abs(close_price - strike) / close_price if close_price > 0 else 1.0
            if dist > near_thr:
                self._log_eval(symbol, label, strike, close_price, 'NA',
                               reason='NOT_NEAR_LEVEL', dist=dist)
                continue

            # Side selection. Default (P-BD off): support->CALL bounce, resistance->PUT
            # rejection. P-BD on: choose the side by price acceptance (breakout/breakdown),
            # skipping a crossed-but-not-accepted level (FALSE_BREAKOUT/BREAKDOWN).
            if config.BREAKOUT_BREAKDOWN_ENABLED:
                sel = _breakout.level_side(level_type, close_price, strike, bar_close=close_price)
                if sel is None:
                    self._log_eval(symbol, label, strike, close_price, 'NA',
                                   reason='FALSE_BREAKOUT_OR_BREAKDOWN', dist=dist)
                    continue
                confirm_type, signal_type, level_action = sel
            else:
                confirm_type = 'PUT' if level_type == 'RESISTANCE' else 'CALL'
                signal_type  = 'BEARISH' if level_type == 'RESISTANCE' else 'BULLISH'
                level_action = ('REJECTION_PUT' if level_type == 'RESISTANCE'
                                else 'BOUNCE_CALL')

            # ── Identify ATM + 1-ITM confirm-side contracts ───────────────────
            ct_keys = [(s, ot) for (s, ot) in opt_data_map if ot == confirm_type]
            if not ct_keys:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='NO_QUOTES', dist=dist)
                continue
            atm_key = min(ct_keys, key=lambda k: abs(k[0] - close_price))
            if confirm_type == 'CALL':
                itm_cands = [k for k in ct_keys if k[0] < close_price and k != atm_key]
                itm_key   = max(itm_cands, key=lambda k: k[0]) if itm_cands else None
            else:
                itm_cands = [k for k in ct_keys if k[0] > close_price and k != atm_key]
                itm_key   = min(itm_cands, key=lambda k: k[0]) if itm_cands else None

            atm_data  = opt_data_map[atm_key]
            atm_delta = vol_deltas.get(atm_key, 0)
            atm_okey: _OptKey = (symbol, atm_key[0], confirm_type)
            atm_low   = self._contract_low_dist(atm_okey, atm_data)
            atm_completed = self._completed_bar(symbol, atm_key[0], confirm_type, expiry,
                                                atm_delta, bar_time)
            atm = self._eval_volume(symbol, list(self._opt_vol_hist[atm_okey]), atm_delta, atm_low,
                                    min_single_vol, min_cluster_vol, cluster_ratio_min,
                                    excitation_min, mark=atm_data.get('mark'), is_atm=True,
                                    next_day_mode=next_day_mode, completed_vol=atm_completed,
                                    peak1m_floor=peak1m_floor, vol3m_floor=vol3m_floor)

            if itm_key:
                itm_data  = opt_data_map[itm_key]
                itm_delta = vol_deltas.get(itm_key, 0)
                itm_okey: _OptKey = (symbol, itm_key[0], confirm_type)
                itm_low   = self._contract_low_dist(itm_okey, itm_data)
                itm_completed = self._completed_bar(symbol, itm_key[0], confirm_type, expiry,
                                                    itm_delta, bar_time)
                itm = self._eval_volume(symbol, list(self._opt_vol_hist[itm_okey]), itm_delta, itm_low,
                                        min_single_vol, min_cluster_vol, cluster_ratio_min,
                                        excitation_min, mark=itm_data.get('mark'), is_atm=False,
                                        next_day_mode=next_day_mode, completed_vol=itm_completed,
                                        peak1m_floor=peak1m_floor, vol3m_floor=vol3m_floor)
            else:
                itm_data, itm_delta, itm_low = {}, 0, None
                itm = self._eval_volume(symbol, [], 0, None, min_single_vol, min_cluster_vol,
                                        cluster_ratio_min, excitation_min,
                                        next_day_mode=next_day_mode,
                                        peak1m_floor=peak1m_floor, vol3m_floor=vol3m_floor)

            valid_volume = atm['valid'] or itm['valid']

            # ── §12 contract-low: hard block a chased ATM (both modes) ─────────
            if atm_low is not None and atm_low > config.CONTRACT_LOW_MAX_DIST:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='CONTRACT_CHASED', dist=dist, atm=atm, low=atm_low)
                continue

            if not valid_volume:
                # Surface the granular blocked reason from the VolumeStickoutScore.
                br = atm.get('block_reason') or itm.get('block_reason') or 'LOW_SCORE'
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason=f'NO_VALID_VOLUME_SIGNAL:{br}', dist=dist, atm=atm, low=atm_low)
                continue

            # Trade the ATM confirm-side contract (nearest spot ≈ the level). The
            # contract we price and trade is the one volume was detected on, so its
            # strike, quote, and alert label stay consistent in every mode. No OTM
            # target-shift — next-day (Tue/Thu) trades at the level just like 0DTE.
            day_mode      = 'NEXT_DAY' if next_day_mode else '0DTE'
            trade_data    = atm_data
            traded_strike = float(atm_key[0])
            target_level: Optional[float] = None

            # ── §13 historical value percentile ───────────────────────────────
            hv_pctile = self._historical_value_pctile(
                symbol, traded_strike, confirm_type, expiry, trade_data)
            if hv_pctile is not None and hv_pctile > config.HIST_VALUE_PCTILE_MAX:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='HISTORICAL_VALUE_TOO_HIGH', dist=dist, atm=atm,
                               low=atm_low, hv=hv_pctile)
                continue

            # ── §13b Premium Discovery Score — fresh footprint vs recycled ─────
            # Distinguishes a first institutional footprint (still cheap, little
            # prior participation) from a spike in a contract whose premium was
            # already discovered. Annotate-only here; the Gold gate enforces
            # eligibility. Stamped onto the signal below for gate_audit + research.
            pds = self._premium_discovery(
                symbol, traded_strike, confirm_type, expiry, trade_data,
                event_volume=atm.get('trigger_volume'))

            # ── §14 short-cover risk (on the volume-bearing ATM contract) ──────
            atm_mark = atm_data.get('mark') or 0.0
            if self._short_cover_risk(symbol, atm_key[0], confirm_type, expiry,
                                      atm_delta, atm_mark, min_single_vol):
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='SHORT_COVER_RISK', dist=dist, atm=atm, low=atm_low)
                continue

            # ── §19 already alerted this direction today ──────────────────────
            if self._fired_today.get((symbol, signal_type)):
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='ALREADY_ALERTED_TODAY', dist=dist, atm=atm, low=atm_low)
                continue

            # ── Passed — build the alert ──────────────────────────────────────
            shape = ('EXTREME_SINGLE' if (atm['A'] or itm['A']) else
                     'CLUSTER'        if (atm['B'] or itm['B']) else 'STAIRSTEP')
            pc_conviction = _pc_conviction(signal_type, pc_ratio)
            timing   = _timing_score(list(self._opt_vol_hist[atm_okey]),
                                     list(self._opt_vol_hist[itm_okey]) if itm_key else [])
            atm_norm = min(1.0, atm['ratio'] / max(config.OPT_CLUSTER_WINDOW_RATIO, 1))
            itm_norm = min(1.0, itm['ratio'] / max(config.OPT_CLUSTER_WINDOW_RATIO, 1))
            cluster_strength = round(0.45 * atm_norm + 0.35 * itm_norm + 0.20 * timing, 4)

            signal = self._build_signal(
                symbol=symbol, level=level, levels=levels, rank=rank, level_label=label,
                level_type=level_type, confirm_type=confirm_type, signal_type=signal_type,
                current_bar=current, expiry=expiry,
                atm_data=atm_data, atm=atm, atm_delta=atm_delta, atm_low_dist=atm_low,
                itm=itm, itm_delta=itm_delta,
                atm_itm_confirm=(atm['valid'] and itm['valid']),
                cluster_strength=cluster_strength, signal_shape=shape,
                hv_pctile=hv_pctile, pc_ratio=pc_ratio, pc_conviction=pc_conviction,
                next_day_mode=next_day_mode, day_mode=day_mode,
                trade_data=trade_data, traded_strike=traded_strike, target_level=target_level,
            )

            # §13b — attach Premium Discovery class + metrics for the Gold gate
            # (gold_mode.classify/production_allowed) and §73 research logging.
            if pds is not None:
                signal['pds_class']    = pds.get('pds_class')
                signal['pds_eligible'] = pds.get('eligible')
                signal['pds']          = pds

            # ── §8-12 Countertrend reversal-conviction gate ───────────────────
            # A candidate that passed every normal gate but OPPOSES a strong, still-
            # working, leadership-confirmed move must clear stricter evidence, else it
            # is held as a watch (does not fire, does not consume the day's allowance).
            ct_decision, ct_reason = 'CONTINUATION', None
            if config.COUNTERTREND_GATE_ENABLED:
                ct_decision, ct_reason = self._countertrend_gate(
                    symbol, signal_type, atm, itm, leadership, bar_time)
            if ct_decision == 'WATCH':
                self._note_countertrend_watch(symbol, signal_type, label, signal, bar_time)
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason=ct_reason, dist=dist, atm=atm, low=atm_low, hv=hv_pctile)
                continue
            if ct_decision == 'REVERSAL':
                signal['signal_context'] = 'PRIMARY_LEVEL_COUNTERTREND_REVERSAL'
                signal['bias'] = 'Countertrend reversal'
                signal['signal_shape'] = signal['flow_shape'] = 'COUNTERTREND_REVERSAL'

            signal['level_action'] = level_action          # P-BD: bounce/rejection/breakout/breakdown

            # Alert taxonomy — Market State × Leadership Type × Direction (+ reasons).
            # Stamped LAST so it reads the final signal_context/flow_shape (the
            # countertrend gate above may have relabeled it REVERSAL) and level_action.
            _taxonomy.classify(
                signal, bars=bars, quotes=option_quotes,
                trend_dir=self._trend.active_direction(symbol),
                trend_working=self._trend.still_working(symbol))
            candidates.append(signal)
            self._record_candidate(symbol, label, strike, close_price, confirm_type,
                                   reason='PASSED', dist=dist, atm=atm, low=atm_low)

        # NOTE: do NOT early-return when `candidates` is empty. The chain-led /
        # Route-B emergent path (below) is designed to fire independent of any
        # primary level — it must run even on polls where no level candidate
        # exists. An earlier `if not candidates: return []` here made chain-led
        # unreachable unless a level candidate coincided on the same poll, which
        # defeated its purpose (non-level ATM flow like TSLA 395P @ 09:05 on
        # 2026-07-09 was never evaluated). The code below is empty-safe.

        # One alert this bar per direction. Across in-range levels keep the
        # strongest: highest-OI level (lowest rank) then largest ATM volume.
        best_by_dir: dict[str, dict] = {}
        for sig in candidates:
            st = sig['signal_type']
            cur = best_by_dir.get(st)
            key = (-sig['level_rank'], sig['atm_vol_1m'])
            if cur is None or key > (-cur['level_rank'], cur['atm_vol_1m']):
                best_by_dir[st] = sig

        fired: list[dict] = []
        for st, sig in best_by_dir.items():
            if self._fired_today.get((symbol, st)):
                continue
            self._fired_today[(symbol, st)] = True
            fired.append(sig)
        # §73 — mark the candidate evals that actually produced an alert.
        fired_labels = {s['level_label'] for s in fired}
        for ev in self.last_candidates:
            if ev['alert_fired'] is False and ev['level_label'] in fired_labels and ev['blocked_reason'] == 'PASSED':
                ev['alert_fired'] = True

        # ── §3-7 Chain-led emergent entries — an additional entry path, but still bound
        # by the one-alert-per-direction-per-day rule: at most one CALL and one PUT
        # alert per symbol per day ACROSS both this and the level-proximity path above
        # (and across restarts, via the durable fired-today fold). Reversals are a
        # separate path (flow_reversal) and are intentionally NOT gated here.
        if config.CHAIN_LED_ENTRY_ENABLED:
            for confirm_type in ('CALL', 'PUT'):
                st = 'BULLISH' if confirm_type == 'CALL' else 'BEARISH'
                if self._fired_today.get((symbol, st)):
                    continue                      # this side already alerted today
                csig, creason = self._chain_led_entry(
                    symbol, confirm_type, opt_data_map, vol_deltas, levels,
                    close_price, expiry, bars, bar_time, bar_time, next_day_mode, None, pc_ratio,
                    leadership=leadership,
                    peak1m_floor=peak1m_floor, vol3m_floor=vol3m_floor)
                if csig is not None:
                    self._fired_today[(symbol, st)] = True
                    fired.append(csig)
                elif creason:
                    logger.info("CHAIN-LED  %s %s  → %s", symbol, confirm_type, creason)

        # ── Fix (2), Option C — opening event-time production promotion (default-off).
        # Promote an opening-window, event-time-eligible contract (frozen ATM±window at
        # event time) to a production entry when its side's opening story is demand-
        # dominant. Reuses the full chain-led/Route-B economic + veto + Gold machinery
        # via force_strike; priced at commit time (no retrospective qualification), and
        # bound by the same one-per-direction-per-day dedup as every other path.
        if (config.OPENING_SCAN_PRODUCTION_ENABLED and opening_range
                and self.last_opening_candidates and option_quotes):
            story = opening_story(symbol, option_quotes, self._event_reg, leadership,
                                  close_price, float(bars[0]['close']) if bars else None)
            for c in self.last_opening_candidates:
                side = c['option_type']
                st = 'BULLISH' if side == 'CALL' else 'BEARISH'
                if self._fired_today.get((symbol, st)):
                    continue
                if not opening_side_confirmed(side, story):
                    continue
                osig, oreason = self._chain_led_entry(
                    symbol, side, opt_data_map, vol_deltas, levels, close_price,
                    expiry, bars, bar_time, bar_time, next_day_mode, None, pc_ratio,
                    leadership=leadership, peak1m_floor=peak1m_floor,
                    vol3m_floor=vol3m_floor, force_strike=c['strike'])
                if osig is not None:
                    osig['opening_event'] = True
                    osig['opening_story'] = story
                    osig['no_retro_label'] = c['no_retro']
                    self._fired_today[(symbol, st)] = True
                    fired.append(osig)
                    logger.info("OPENING-PROD fired %s %s $%.2f  story=%s  retro=%s",
                                symbol, side, c['strike'], story, c['no_retro'])
                elif oreason:
                    logger.info("OPENING-PROD %s %s $%.2f → %s  (story=%s)",
                                symbol, side, c['strike'], oreason, story)

        # ── V2 chain-leadership — did one side seize the chain? ───────────────────
        # Measures COORDINATED cross-strike control over the wide watched window (not a
        # single-strike threshold), then trades (or SHADOW-records) the recommended
        # convexity contract. Catches a "one event spread across five strikes" move
        # (GOOGL 357.5-365C) the ATM±1 window + per-strike floors never saw. Shadow mode
        # runs the same scan/entry but records the would-be signal instead of firing it.
        if (config.CHAIN_LEADERSHIP_ENABLED or config.CHAIN_LEADERSHIP_SHADOW) and opt_data_map:
            production = config.CHAIN_LEADERSHIP_ENABLED
            verdict = self._chain_leadership_scan(symbol, opt_data_map, vol_deltas,
                                                  close_price, bar_time, expiry)
            side = verdict['controlling_side']
            if side and verdict['confidence'] >= config.CHAIN_LEADERSHIP_MIN_CONFIDENCE:
                st = 'BULLISH' if side == 'CALL' else 'BEARISH'
                dedup = self._fired_today if production else self._leadership_shadow_fired
                if not dedup.get((symbol, st)):
                    lsig, lreason = self._chain_leadership_entry(
                        symbol, verdict, opt_data_map, vol_deltas, levels, close_price,
                        expiry, bars, bar_time, bar_time, next_day_mode, pc_ratio)
                    if lsig is not None and production:
                        self._fired_today[(symbol, st)] = True
                        fired.append(lsig)
                        logger.info("CHAIN-LEADERSHIP fired %s %s  leader=%s rec=%s breadth=%d "
                                    "notional=$%d conf=%d  chain=%s", symbol, side,
                                    verdict['leader_strike'], verdict['recommended_strike'],
                                    verdict['breadth'], verdict['combined_notional'],
                                    verdict['confidence'], verdict['supporting_strikes'])
                    elif lsig is not None:                 # shadow: record, do not fire
                        self._leadership_shadow_fired[(symbol, st)] = True
                        lsig['shadow'] = True
                        lsig['leadership_spot'] = close_price
                        self.last_leadership_shadow.append(lsig)
                        logger.info("CHAIN-LEADERSHIP SHADOW %s %s  rec=%s @$%s breadth=%d "
                                    "notional=$%d conf=%d  chain=%s", symbol, side,
                                    verdict['recommended_strike'], lsig.get('price_to_enter'),
                                    verdict['breadth'], verdict['combined_notional'],
                                    verdict['confidence'], verdict['supporting_strikes'])
                    elif lreason:
                        logger.info("CHAIN-LEADERSHIP%s %s %s → %s",
                                    "" if production else " SHADOW", symbol, side, lreason)
            elif side:
                logger.debug("CHAIN-LEADERSHIP %s %s conf=%d < %d (breadth=%d)",
                             symbol, side, verdict['confidence'],
                             config.CHAIN_LEADERSHIP_MIN_CONFIDENCE, verdict['breadth'])
        return fired

    # ── Per-contract volume evaluation — ENTRY VOLUME GATE FIX (3-rule) ─────────

    def _eval_volume(
        self,
        symbol: str,
        history: list[int],
        delta: int,
        low_dist: Optional[float],
        min_single: int,
        min_cluster_vol: int,
        cluster_ratio_min: float,
        excitation_min: float,
        *,
        mark: Optional[float] = None,
        is_atm: bool = False,
        next_day_mode: bool = False,
        completed_vol: Optional[int] = None,
        peak1m_floor: Optional[int] = None,
        vol3m_floor: Optional[int] = None,
    ) -> dict:
        """
        PRODUCTION VOLUME GATE (two-path) — absolute volume is the binding
        requirement; a high ratio is NEVER sufficient on its own.

          Path A DOMINANT ABSOLUTE      — a very large event qualifies on size +
                                          concentration + near-low + notional.
          Path B CONTEXTUAL CONVICTION  — moderate size qualifies only with extreme
                                          ratio + concentrated event + near contract
                                          low + meaningful premium notional (the
                                          caller has already enforced primary-level
                                          proximity, correct side, and ATM/1-ITM).

        `history` is per-minute volume deltas oldest→newest, current bar (`delta`) last.
        `completed_vol` (§7-8) is the closed 1-min bar volume when available; it is
        preferred over the live poll-delta as the trigger. The legacy single/cluster/
        stair booleans are kept for shape labels + logging only — they no longer decide
        `valid` (§14: no alternate ratio-led path).
        """
        volatile  = symbol in config.VOLATILE_SYMBOLS
        vol_floor = 250 if volatile else 100      # single-bar current-volume floor
        win_floor = 600 if volatile else 300      # 5-bar window floor

        # ── Single-bar inputs (median-robust baseline + visual dominance) ─────
        prior20  = history[-21:-1] if len(history) > 1 else []
        prior10  = history[-11:-1] if len(history) > 1 else []
        median20 = statistics.median(prior20) if prior20 else 0.0
        max20    = max(prior20) if prior20 else 0.0
        avg10    = (sum(prior10) / len(prior10)) if prior10 else 0.0
        baseline = max(avg10, median20, 10.0)
        vol_ratio   = delta / baseline
        visual_dom  = delta / max(max20, 1.0)

        # ── 5-bar window / cluster inputs (median of prior rolling windows) ───
        last5 = history[-5:]
        win5  = sum(last5)
        prior_windows = [sum(history[i:i + 5]) for i in range(0, max(0, len(history) - 5))][-20:]
        med_win = statistics.median(prior_windows) if prior_windows else 0.0
        max_win = max(prior_windows) if prior_windows else 0.0
        win_ratio5  = win5 / max(med_win, 50.0)
        cluster_dom = win5 / max(max_win, 1.0)
        active5 = sum(1 for v in last5 if v >= max(median20 * 2.0, 50.0))

        clu        = _cluster_metrics(history)
        excitation = _excitation(clu['window'], clu['base_unit'])

        # ════════════════════════════════════════════════════════════════════
        # PRODUCTION TWO-PATH GATE (§1-6,10-12,14,19)
        # ════════════════════════════════════════════════════════════════════
        # §7-8 partial vs completed bar: prefer the closed 1-min bar volume as the
        # trigger when it is larger (e.g. 456 observed → 508 completed).
        observed_vol = int(delta)
        peak1m = int(delta)
        if completed_vol is not None:
            bar_status = 'REVISED' if completed_vol != observed_vol else 'COMPLETED'
            peak1m = max(peak1m, int(completed_vol))
        else:
            bar_status = 'PARTIAL'

        vol3m = sum(history[-3:-1]) + peak1m          # last 3 min, current bar = peak1m
        vol5m = sum(history[-5:-1]) + peak1m

        # Concentration + background quality (reuse the reversal event metrics).
        ev = volume_event(history)
        event_share   = ev['share'] if ev else round(peak1m / max(vol5m, 1), 3)
        persistent_bg = bool(ev['persistent_bg']) if ev else False

        single_ratio = round(peak1m / baseline, 2)
        window_ratio = round(clu['ratio'], 2)         # WindowRatio5

        def _grp(d):
            return d.get(symbol, d['default'])
        notional_min = (config.MINIMUM_PREMIUM_NOTIONAL_NEXT_EXPIRY if next_day_mode
                        else config.MINIMUM_PREMIUM_NOTIONAL_0DTE)

        # Qualifying shape for the Path-B base floors + its event-share requirement.
        if peak1m >= config.SINGLE_PRINT_BASE_FLOOR:
            shape, trig_vol, share_min = 'SINGLE_BAR', peak1m, config.SINGLE_PRINT_EVENT_SHARE_MIN
        elif vol3m >= config.THREE_MINUTE_BASE_FLOOR:
            shape, trig_vol, share_min = 'THREE_MIN', vol3m, config.THREE_MINUTE_EVENT_SHARE_MIN
        elif vol5m >= config.FIVE_MINUTE_BASE_FLOOR:
            shape, trig_vol, share_min = 'FIVE_MIN', vol5m, config.FIVE_MINUTE_EVENT_SHARE_MIN
        else:
            shape, trig_vol, share_min = 'SINGLE_BAR', peak1m, config.SINGLE_PRINT_EVENT_SHARE_MIN
        base_vol_ok = (peak1m >= config.SINGLE_PRINT_BASE_FLOOR
                       or vol3m >= config.THREE_MINUTE_BASE_FLOOR
                       or vol5m >= config.FIVE_MINUTE_BASE_FLOOR)

        premium_notional = round(trig_vol * (mark or 0.0) * 100.0)
        notional_ok  = premium_notional >= notional_min
        near_low_ctx = (low_dist is None or low_dist <= config.CONTEXTUAL_LOW_DIST_MAX)   # ≤1.50
        near_low_dom = (low_dist is None or low_dist <= config.NEAR_LOW_MAX_DIST)         # ≤1.75

        # ── Path A — Dominant absolute volume (§3) ────────────────────────────
        dom_vol_ok = (peak1m >= _grp(config.DOMINANT_SINGLE_PRINT)
                      or vol3m >= _grp(config.DOMINANT_3M)
                      or vol5m >= _grp(config.DOMINANT_5M))
        path_a = (config.TRUE_CONVICTION_GATE_ENABLED and dom_vol_ok
                  and event_share >= config.DOMINANT_EVENT_SHARE_MIN
                  and not persistent_bg and near_low_dom and notional_ok)

        # ── Path B — Contextual level conviction (§4) ─────────────────────────
        # NearPrimaryLevel + correct side + ATM/1-ITM are enforced by the caller.
        ratio_ok = (single_ratio >= config.CONTEXTUAL_SINGLE_PRINT_RATIO
                    or window_ratio >= config.CONTEXTUAL_MULTI_BAR_RATIO)
        path_b = (config.CONTEXTUAL_LEVEL_CONVICTION_ENABLED and base_vol_ok and ratio_ok
                  and near_low_ctx and event_share >= share_min
                  and not persistent_bg and notional_ok)

        # ── Mandatory tightened floor (§ tighten patch) — binds BOTH paths ────
        # An event must clear at least one absolute floor regardless of ratio or the
        # dominant path. Opening-window floors (stricter) are passed by the caller.
        pk_floor = peak1m_floor if peak1m_floor is not None else config.PEAK_1M_VOLUME_MIN
        v3_floor = vol3m_floor  if vol3m_floor  is not None else config.VOLUME_3M_MIN
        mandatory_floor_ok = (peak1m >= pk_floor) or (vol3m >= v3_floor)

        valid = bool(mandatory_floor_ok and (path_a or path_b))
        path  = 'A' if path_a else ('B' if path_b else None)

        # ── Gold-standard quality label (§5) ──────────────────────────────────
        gold_standard = bool(
            path_b and low_dist is not None and low_dist <= config.GOLD_STANDARD_LOW_DIST
            and is_atm and single_ratio >= config.CONTEXTUAL_SINGLE_PRINT_RATIO)
        classification = (['GOLD_STANDARD_ALERT', 'PRIMARY_LEVEL_CONVICTION',
                           'ATM_LEVEL_MATCH', 'ENTRY_NEAR_CONTRACT_LOW']
                          if gold_standard else [])

        # ── §8 PENDING_VOLUME_CONFIRMATION — near-miss on a still-partial bar ──
        # Within tolerance of the single-print floor AND the bar has not closed AND
        # every other contextual condition already passes → hold, do not reject;
        # the next poll (or the completed bar) re-evaluates.
        ctx_ok = (ratio_ok and near_low_ctx and not persistent_bg
                  and event_share >= config.SINGLE_PRINT_EVENT_SHARE_MIN
                  and round(peak1m * (mark or 0.0) * 100.0) >= notional_min)
        pending = bool(
            not valid and not mandatory_floor_ok and bar_status == 'PARTIAL'
            and config.CONTEXTUAL_LEVEL_CONVICTION_ENABLED and ctx_ok
            and peak1m >= (1.0 - config.PENDING_VOLUME_TOLERANCE_PCT) * pk_floor)

        # ── Block reason (§6 spam first, then the specific failing condition) ──
        if valid:
            block_reason = 'OK'
        elif pending:
            block_reason = 'PENDING_VOLUME_CONFIRMATION'
        elif not mandatory_floor_ok:
            block_reason = 'RESEARCH_ONLY_SUBTHRESHOLD_EVENT'     # below tightened floor
        elif not base_vol_ok:
            block_reason = 'INSUFFICIENT_CONVICTION_VOLUME'       # small vol + (any) ratio
        elif persistent_bg:
            block_reason = 'PERSISTENT_BACKGROUND_FLOW'
        elif not notional_ok:
            block_reason = 'LOW_PREMIUM_NOTIONAL'
        elif not near_low_ctx:
            block_reason = 'CONTRACT_NOT_NEAR_LOW'
        elif event_share < share_min:
            block_reason = 'LOW_EVENT_SHARE'
        elif not ratio_ok:
            block_reason = 'LOW_RELATIVE_VOLUME'
        else:
            block_reason = 'NO_CONVICTION_PATH'

        trig_type  = 'SINGLE_BAR' if shape == 'SINGLE_BAR' else 'MULTI_MIN_WINDOW'
        trig_ratio = single_ratio if shape == 'SINGLE_BAR' else window_ratio

        # Legacy shape booleans (labels/logging only — not an alert path).
        single_valid  = peak1m >= config.SINGLE_PRINT_BASE_FLOOR and single_ratio >= 8.0
        cluster_valid = vol5m >= config.FIVE_MINUTE_BASE_FLOOR and window_ratio >= 3.0
        stair_valid   = vol5m >= config.FIVE_MINUTE_BASE_FLOOR and excitation >= excitation_min

        return {
            'delta': delta, 'spike_ratio': single_ratio,
            'vol': vol5m, 'ratio': window_ratio,
            'peak_1m': peak1m, 'vol_3m': vol3m, 'vol_5m': vol5m,
            'peak1m_floor': pk_floor, 'vol3m_floor': v3_floor,
            'mandatory_floor_ok': mandatory_floor_ok,
            'event_share': event_share, 'persistent_bg': persistent_bg,
            'premium_notional': premium_notional,
            'observed_vol': observed_vol, 'completed_vol': completed_vol,
            'bar_status': bar_status, 'shape': shape, 'pending': pending,
            'path': path, 'gold_standard': gold_standard, 'classification': classification,
            'active': active5, 'burst': clu['burst'], 'excitation': excitation,
            'visual_dom': round(visual_dom, 2), 'cluster_dom': round(cluster_dom, 2),
            'A': bool(single_valid), 'B': bool(cluster_valid), 'C': bool(stair_valid),
            'strong': bool(gold_standard or path_a),
            'block_reason': block_reason,
            'trigger_type': trig_type, 'trigger_volume': trig_vol,
            'trigger_ratio': trig_ratio,
            'valid': valid,
        }

    # ── §13 historical value percentile ───────────────────────────────────────

    def _historical_value_pctile(
        self, symbol: str, strike: float, opt_type: str,
        expiry: Optional[date], data: dict,
    ) -> Optional[float]:
        """
        (mark - HistLow) / (HistHigh - HistLow) over the multi-day window, or None
        when not evaluable (no fn, no expiry, no mark, or no prior history → 0DTE).
        """
        if expiry is None:
            return None
        mark = data.get('mark')
        if not mark or mark <= 0:
            return None
        occ = occ_symbol(symbol, expiry, strike, opt_type)
        if occ not in self._opt_hist_range:
            rng = None
            if self._hist_range_fn is not None:
                try:
                    rng = self._hist_range_fn(occ)
                except Exception as exc:
                    logger.warning("hist_range_fn failed for %s: %s", occ, exc)
            # Fallback: no live multi-day history → previous session's (low, high).
            if not rng and self._prev_range_fn is not None:
                try:
                    rng = self._prev_range_fn(symbol, strike, opt_type, self._history_date)
                    if rng:
                        logger.debug("§13 hist-range fallback for %s → prev-session %s", occ, rng)
                except Exception as exc:
                    logger.warning("prev_range_fn failed for %s: %s", occ, exc)
            self._opt_hist_range[occ] = rng
        rng = self._opt_hist_range.get(occ)
        if not rng:
            return None
        low, high = rng
        span = high - low
        if span <= 0:
            span = 0.01
        return round((mark - low) / span, 4)

    # ── §13b Premium Discovery Score ──────────────────────────────────────────

    def _premium_discovery(
        self, symbol: str, strike: float, opt_type: str,
        expiry: Optional[date], data: dict, *, event_volume,
    ) -> Optional[dict]:
        """
        Classify the contract's premium-discovery state (fresh vs recycled) from its
        historical premium/volume distribution. Returns premium_discovery.score()'s
        dict, or None when not evaluable (gate off, no fn, no mark). The per-contract
        history is fetched once per day and cached, mirroring the §13 range cache.
        """
        if not config.PREMIUM_DISCOVERY_GATE_ENABLED:
            return None
        if self._premium_hist_fn is None:
            return None
        mark = data.get('mark')
        if not mark or mark <= 0:
            return None
        occ = occ_symbol(symbol, expiry, strike, opt_type)
        if occ not in self._opt_premium_hist:
            bars = []
            try:
                bars = self._premium_hist_fn(
                    symbol, strike, opt_type, self._history_date) or []
            except Exception as exc:
                logger.warning("premium_hist_fn failed for %s: %s", occ, exc)
            self._opt_premium_hist[occ] = bars
        bars = self._opt_premium_hist.get(occ) or []
        try:
            return _premium_discovery.score(bars, mark, event_volume)
        except Exception as exc:
            logger.warning("premium_discovery.score failed for %s: %s", occ, exc)
            return None

    # ── §14 short-cover risk ──────────────────────────────────────────────────

    def _short_cover_risk(
        self, symbol: str, strike: float, opt_type: str, expiry: Optional[date],
        cur_vol: int, cur_price: float, min_major_vol: int,
    ) -> bool:
        """
        True when the current major volume event mirrors an earlier event of
        similar size but at a much lower price (shorts covering, not fresh longs).

        Only "major" events (cur_vol >= min_major_vol) are compared and stored.
        """
        if not config.SHORT_COVER_FILTER:
            return False
        if cur_vol < min_major_vol or cur_price <= 0:
            return False
        key = f"{symbol}|{expiry}|{strike}|{opt_type}"
        risk = False
        for prior_vol, prior_price in self._major_events[key]:
            if prior_price <= 0:
                continue
            sim = cur_vol / max(prior_vol, 1)
            if config.SHORT_COVER_SIM_LOW <= sim <= config.SHORT_COVER_SIM_HIGH:
                if cur_price / prior_price <= config.SHORT_COVER_REPRICE_MAX:
                    risk = True
                    break
        self._major_events[key].append((cur_vol, cur_price))
        return risk

    # ── Opposite-side volume validation (exit state machine) ─────────────────

    def check_opposite_side(
        self,
        symbol: str,
        signal_type: str,
        option_quotes: dict,
        target_price: float,
    ) -> bool:
        """
        True if opposite-side volume shows cluster-level activity near target_price.
        Used by check_exits() for early exit2. BULLISH→watch PUTs, BEARISH→CALLs.
        """
        opp_type      = 'PUT' if signal_type == 'BULLISH' else 'CALL'
        min_spike_vol = config.OPT_MIN_SPIKE_VOL.get(symbol, config.OPT_MIN_SPIKE_VOL['default'])
        min_clust_vol = config.OPT_MIN_CLUSTER_VOL.get(symbol, config.OPT_MIN_CLUSTER_VOL['default'])

        opp_keys = sorted(
            [(s, ot) for (s, ot) in option_quotes if ot == opp_type],
            key=lambda k: abs(k[0] - target_price),
        )
        if not opp_keys:
            return False

        def _side_active(key: tuple) -> bool:
            okey: _OptKey = (symbol, key[0], opp_type)
            hist = list(self._opt_vol_hist[okey])
            if not hist:
                return False
            delta              = hist[-1]
            ratio              = _spike_ratio(hist, delta)
            win_vol, win_ratio = _window_ratio(hist)
            burst   = delta >= min_spike_vol and ratio     >= config.OPT_SINGLE_SPIKE_RATIO
            cluster = win_ratio >= config.OPT_CONSEC_SPIKE_RATIO and win_vol >= min_clust_vol
            return burst or cluster

        atm_active = _side_active(opp_keys[0])
        itm_active = _side_active(opp_keys[1]) if len(opp_keys) > 1 else False
        result     = atm_active and itm_active
        if result:
            logger.info("OppSide ACTIVE  %s %s  opp=%s  near=%.2f",
                        symbol, signal_type, opp_type, target_price)
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _completed_bar(self, symbol, strike, ot, expiry, observed, bar_time) -> Optional[int]:
        """
        §7-8 — the closed 1-min OPRA bar volume for a contract, or None.

        Only queried when the live poll-delta (`observed`) is already promising
        (≥ half the single-print floor) so we don't spend an API call per contract
        per poll on obvious non-events. Cached per contract per minute.
        """
        if self._completed_bar_fn is None or expiry is None:
            return None
        if observed < 0.5 * config.SINGLE_PRINT_BASE_FLOOR:
            return None
        occ  = occ_symbol(symbol, expiry, strike, ot)
        ckey = (occ, bar_time.replace(second=0, microsecond=0))
        if ckey in self._completed_bar_cache:
            return self._completed_bar_cache[ckey]
        vol = None
        try:
            vol = self._completed_bar_fn(occ, bar_time)
        except Exception as exc:
            logger.warning("completed_bar_fn failed for %s: %s", occ, exc)
        self._completed_bar_cache[ckey] = vol
        return vol

    def _contract_low_dist(self, okey: _OptKey, data: dict) -> Optional[float]:
        """
        §12 ContractLowDistance = mark / max(IntradayLow, 0.01); None if unknown.
        Uses the lower of Schwab's day_low and our watched-session min.
        """
        mark = data.get('mark')
        candidates = [x for x in (self._opt_mark_low.get(okey), data.get('day_low'))
                      if x and x > 0]
        low = min(candidates) if candidates else None
        if mark and low and low > 0:
            return round(mark / low, 4)
        return None

    def _strike_metrics(self, symbol, key, quote, delta, bar_time, expiry) -> dict:
        """Per-strike chain metrics (peak1m incl. completed bar, 3m/5m, share, bg, notional)."""
        strike, ot = key
        hist = list(self._opt_vol_hist.get((symbol, strike, ot), []))
        completed = self._completed_bar(symbol, strike, ot, expiry, int(delta or 0), bar_time)
        peak1m = max(int(delta or 0), int(completed)) if completed is not None else int(delta or 0)
        vol3m = sum(hist[-3:-1]) + peak1m
        vol5m = sum(hist[-5:-1]) + peak1m
        ev = volume_event(hist)
        share         = ev['share'] if ev else round(peak1m / max(vol5m, 1), 3)
        event_vol     = ev['event_vol'] if ev else peak1m
        persistent_bg = bool(ev['persistent_bg']) if ev else False
        low_dist = self._contract_low_dist((symbol, strike, ot), quote)
        mark     = quote.get('mark') or 0.0
        notional = round(max(peak1m, vol3m) * mark * 100.0)
        return dict(key=key, strike=strike, ot=ot, peak1m=peak1m, vol3m=vol3m, vol5m=vol5m,
                    share=share, event_vol=event_vol, persistent_bg=persistent_bg,
                    low_dist=low_dist, mark=mark, notional=notional, delta=int(delta or 0))

    @staticmethod
    def _target_oi_name(price, levels) -> Optional[str]:
        """Map a target price back to its morning OI rank label (R1..S3), §17."""
        if price is None:
            return None
        best, bestd = None, 1e9
        for lv in levels:
            d = abs(float(lv['strike']) - float(price))
            if d < bestd:
                bestd, best = d, lv
        if best is None:
            return None
        side = 'R' if best['level_type'] == 'RESISTANCE' else 'S'
        return f"{side}{best.get('rank', '')}".rstrip()

    def _chain_led_entry(self, symbol, confirm_type, opt_data_map, vol_deltas, levels,
                         close_price, expiry, bars, bar_time, now, next_day_mode,
                         hv_pctile, pc_ratio, leadership=None,
                         peak1m_floor=None, vol3m_floor=None, force_strike=None):
        """
        §3-7 — chain-led emergent entry. Coordinated ATM + adjacent-strike volume builds a
        new emergent support/resistance before spot reaches a primary level. Returns
        (signal_dict | None, block_reason | None). Additive to the primary-level path.
        """
        if not config.CHAIN_LED_ENTRY_ENABLED:
            return None, None
        signal_type = 'BULLISH' if confirm_type == 'CALL' else 'BEARISH'
        # Same one-per-direction-per-day key as the level-proximity path, so a
        # chain-led alert never duplicates a side that already fired today.
        if self._fired_today.get((symbol, signal_type)):
            return None, None

        ct = [(s, ot) for (s, ot) in opt_data_map if ot == confirm_type]
        if len(ct) < 2:
            return None, 'CHAIN_CONFIRMATION_MISSING'
        if force_strike is not None:
            # Fix (2): opening event-time promotion anchors the ATM to the FROZEN
            # event-time strike (which may differ from live-ATM after spot moved),
            # not live proximity — the whole point of event-time eligibility.
            atm_key = next((k for k in ct if abs(k[0] - float(force_strike)) < 1e-6), None)
            if atm_key is None:
                return None, 'OPENING_STRIKE_NOT_QUOTED'
        else:
            atm_key = min(ct, key=lambda k: abs(k[0] - close_price))
        a_strike = atm_key[0]
        if confirm_type == 'CALL':           # ITM below spot, OTM above
            itm = [k for k in ct if k[0] < a_strike]; otm = [k for k in ct if k[0] > a_strike]
            itm_key = max(itm, key=lambda k: k[0]) if itm else None
            otm_key = min(otm, key=lambda k: k[0]) if otm else None
        else:                                # PUT: ITM above spot, OTM below
            itm = [k for k in ct if k[0] > a_strike]; otm = [k for k in ct if k[0] < a_strike]
            itm_key = min(itm, key=lambda k: k[0]) if itm else None
            otm_key = max(otm, key=lambda k: k[0]) if otm else None

        def m(key):
            return (self._strike_metrics(symbol, key, opt_data_map[key],
                                         vol_deltas.get(key, 0), bar_time, expiry)
                    if key is not None else None)
        atm_m, itm_m, otm_m = m(atm_key), m(itm_key), m(otm_key)
        chain = [x for x in (itm_m, atm_m, otm_m) if x]

        # §4D individual strike quality
        atm_ok = atm_m['peak1m'] >= config.CHAIN_ATM_1M_MIN or atm_m['vol3m'] >= config.CHAIN_ATM_3M_MIN
        adj = [x for x in (itm_m, otm_m)
               if x and (x['peak1m'] >= config.CHAIN_ADJACENT_1M_MIN
                         or x['vol3m'] >= config.CHAIN_ADJACENT_3M_MIN)]
        if not atm_ok:
            return None, 'CHAIN_VOLUME_INSUFFICIENT'
        if not adj:                                   # §4A multi-strike confirmation
            # Route B (§ P3): an exceptional single ATM strike can stand in for adjacent
            # confirmation. Only in Gold mode (else chain-led behavior is unchanged);
            # opposite-side dominance is still enforced by the leadership gate below.
            route_b = (config.GOLD_ONLY_PRODUCTION_MODE and route_b_qualifies(
                peak_1m=atm_m['peak1m'], strikes_from_atm=0,
                premium_notional=atm_m['notional'],
                clow_region=contract_low_region(atm_m['low_dist']),
                concentrated=(atm_m['share'] >= config.CHAIN_EVENT_SHARE_MIN),
                opposite_dominates=False))
            if not route_b:
                return None, 'CHAIN_CONFIRMATION_MISSING'

        # §4C combined absolute volume (ITM+ATM+OTM)
        c1, c3, c5 = (sum(x['peak1m'] for x in chain), sum(x['vol3m'] for x in chain),
                      sum(x['vol5m'] for x in chain))

        # Mandatory tightened floor (§ tighten patch) — the combined chain event must
        # clear the same absolute floor as the primary path (opening-aware, binding).
        pk_floor = peak1m_floor if peak1m_floor is not None else config.PEAK_1M_VOLUME_MIN
        v3_floor = vol3m_floor  if vol3m_floor  is not None else config.VOLUME_3M_MIN
        if not (c1 >= pk_floor or c3 >= v3_floor):
            return None, 'RESEARCH_ONLY_SUBTHRESHOLD_EVENT'
        if confirm_type == 'CALL':
            f1, f3, f5 = (config.CHAIN_CALL_COMBINED_1M_FLOOR, config.CHAIN_CALL_COMBINED_3M_FLOOR,
                          config.CHAIN_CALL_COMBINED_5M_FLOOR)
        else:
            f1, f3, f5 = (config.CHAIN_PUT_COMBINED_1M_FLOOR, config.CHAIN_PUT_COMBINED_3M_FLOOR,
                          config.CHAIN_PUT_COMBINED_5M_FLOOR)
        if not (c1 >= f1 or c3 >= f3 or c5 >= f5):
            return None, 'CHAIN_VOLUME_INSUFFICIENT'

        # §4F economic size
        comb_notional = sum(x['notional'] for x in chain)
        if not (comb_notional >= config.CHAIN_COMBINED_NOTIONAL_MIN
                and atm_m['notional'] >= config.CHAIN_ATM_NOTIONAL_MIN):
            return None, 'CHAIN_NOTIONAL_INSUFFICIENT'

        # §4E contract value location
        if atm_m['low_dist'] is not None and atm_m['low_dist'] > config.CHAIN_ATM_LOW_DISTANCE_MAX:
            return None, 'EMERGENT_ENTRY_CHASED'
        if not any(x['low_dist'] is None or x['low_dist'] <= config.CHAIN_ADJACENT_LOW_DISTANCE_MAX
                   for x in adj):
            return None, 'EMERGENT_ENTRY_CHASED'

        # §4G flow concentration
        concentrated = sum(1 for x in chain if x['share'] >= config.CHAIN_EVENT_SHARE_MIN)
        tot20 = sum(x['event_vol'] / max(x['share'], 0.01) for x in chain)
        comb_share = sum(x['event_vol'] for x in chain) / max(tot20, 1.0)
        if not (concentrated >= 2 or comb_share >= config.CHAIN_COMBINED_EVENT_SHARE_MIN):
            return None, 'CHAIN_VOLUME_INSUFFICIENT'

        # §4H background quality
        if atm_m['persistent_bg']:
            return None, 'CHAIN_VOLUME_INSUFFICIENT'

        # §4I directional leadership (computed once per poll, passed in)
        ld = leadership if leadership is not None else compute_leadership_scores(
            symbol, opt_data_map, self._opt_vol_hist, self._contract_low_dist)
        call_ld = (ld or {}).get('call_leadership', 0.0)
        put_ld  = (ld or {}).get('put_leadership', 0.0)
        lead, opp = (call_ld, put_ld) if confirm_type == 'CALL' else (put_ld, call_ld)
        if not (lead >= config.CHAIN_LEADERSHIP_MIN and (lead - opp) >= config.CHAIN_LEADERSHIP_MARGIN):
            return None, 'CHAIN_LEADERSHIP_INSUFFICIENT'

        # §7 contract selection — ATM default; 1-OTM only if independently strong + near low.
        selected = atm_m
        if (otm_m and otm_m in adj and otm_m['low_dist'] is not None
                and otm_m['low_dist'] <= config.CHAIN_ATM_LOW_DISTANCE_MAX
                and (otm_m['peak1m'] >= config.CHAIN_ATM_1M_MIN or otm_m['vol3m'] >= config.CHAIN_ATM_3M_MIN)):
            selected = otm_m
        # §4J selected not already chased
        if selected['low_dist'] is not None and selected['low_dist'] > config.CHAIN_SELECTED_LOW_DISTANCE_MAX:
            return None, 'EMERGENT_ENTRY_CHASED'

        # ── Build the emergent signal (reuse _build_signal + price-order targets §16) ──
        sel_key = selected['key']
        sel_quote = opt_data_map[sel_key]
        other_m = next((x for x in (atm_m, itm_m, otm_m) if x and x is not selected), atm_m)
        sel_ev = self._eval_volume(symbol, list(self._opt_vol_hist.get((symbol, sel_key[0], confirm_type), [])),
                                   selected['delta'], selected['low_dist'], 300, 300, 3.0,
                                   config.STAIRSTEP_EXCITATION_MIN, mark=selected['mark'],
                                   is_atm=(selected is atm_m), next_day_mode=next_day_mode)
        oth_ev = self._eval_volume(symbol, list(self._opt_vol_hist.get((symbol, other_m['key'][0], confirm_type), [])),
                                   other_m['delta'], other_m['low_dist'], 300, 300, 3.0,
                                   config.STAIRSTEP_EXCITATION_MIN, mark=other_m['mark'], is_atm=False,
                                   next_day_mode=next_day_mode)

        # Emergent location spot = underlying close ~window bars back (§6).
        n = config.CHAIN_CONFIRMATION_WINDOW_MINUTES + 1
        emergent_spot = float(bars[-n]['close']) if len(bars) >= n else float(close_price)
        loc_type = 'SUPPORT' if confirm_type == 'CALL' else 'RESISTANCE'
        day_mode = 'NEXT_DAY' if next_day_mode else '0DTE'

        sig = self._build_signal(
            symbol, {'strike': float(sel_key[0])}, levels, 0, 'EMERGENT', loc_type,
            confirm_type, signal_type, {'close': close_price}, expiry, sel_quote, sel_ev,
            selected['delta'], selected['low_dist'], oth_ev, other_m['delta'], True,
            round(lead, 3), 'CHAIN_LED', hv_pctile, pc_ratio,
            _pc_conviction(signal_type, pc_ratio), next_day_mode, day_mode, sel_quote,
            float(sel_key[0]), None)

        # Chain-led overrides + emergent metadata (persisted by the caller).
        sig['signal_context'] = 'CHAIN_LED_EMERGENT_ENTRY'
        sig['signal_shape'] = sig['flow_shape'] = 'CHAIN_LED'
        sig['level_label'] = 'EMERGENT'
        sig['level_price'] = emergent_spot
        sig['bias'] = 'Chain-led call' if confirm_type == 'CALL' else 'Chain-led put'
        sig['target1_oi_name'] = self._target_oi_name(sig.get('exit1_price'), levels)
        sig['target2_oi_name'] = self._target_oi_name(sig.get('exit2_price'), levels)
        sig['emergent_location_id'] = None
        sig['emergent'] = {
            'session_date': now.date(), 'symbol': symbol, 'location_type': loc_type,
            'location_spot': emergent_spot, 'direction': signal_type,
            'event_start': bar_time, 'event_end': bar_time,
            'atm_strike': atm_m['strike'], 'itm_strike': itm_m['strike'] if itm_m else None,
            'otm_strike': otm_m['strike'] if otm_m else None,
            'atm_vol_3m': atm_m['vol3m'], 'itm_vol_3m': itm_m['vol3m'] if itm_m else None,
            'otm_vol_3m': otm_m['vol3m'] if otm_m else None, 'combined_vol_3m': c3,
            'atm_notional': atm_m['notional'], 'combined_notional': comb_notional,
            'atm_low_dist': atm_m['low_dist'], 'itm_low_dist': itm_m['low_dist'] if itm_m else None,
            'otm_low_dist': otm_m['low_dist'] if otm_m else None,
            'call_leadership': round(call_ld, 3), 'put_leadership': round(put_ld, 3),
            'selected_strike': float(sel_key[0]),
        }
        # Chain-led trigger display fields (§18).
        sig['chain_strikes'] = [x['strike'] for x in chain]
        sig['chain_combined_3m'] = c3
        sig['emergent_spot'] = emergent_spot

        # §13b — Premium Discovery on the SELECTED chain contract, so the Gold gate
        # applies the same fresh-vs-recycled requirement to emergent entries. Event
        # volume is the selected strike's concentrated event volume.
        pds = self._premium_discovery(
            symbol, float(sel_key[0]), confirm_type, expiry, sel_quote,
            event_volume=selected['event_vol'])
        if pds is not None:
            sig['pds_class']    = pds.get('pds_class')
            sig['pds_eligible'] = pds.get('eligible')
            sig['pds']          = pds

        # Alert taxonomy — chain-led emergent entries carry CHAIN_LED context/shape,
        # so leadership resolves to Chain Leader; market state from bars + trend.
        _taxonomy.classify(
            sig, bars=bars, quotes=opt_data_map,
            trend_dir=self._trend.active_direction(symbol),
            trend_working=self._trend.still_working(symbol))
        return sig, None

    def _chain_leadership_scan(self, symbol, opt_data_map, vol_deltas, close_price,
                               bar_time, expiry):
        """
        V2 chain-leadership scan: build per-side contract lists across the WIDE watched
        window (strike, event volume, notional, mark, cheapness) and detect cross-strike
        CALL/PUT control via analysis.chain_leadership.detect(). Returns the verdict dict.
        """
        def _contracts(ot):
            out = []
            for (s, o), q in opt_data_map.items():
                if o != ot:
                    continue
                m = self._strike_metrics(symbol, (s, ot), q, vol_deltas.get((s, ot), 0),
                                         bar_time, expiry)
                out.append({'strike': float(s), 'vol': int(m['vol3m']),
                            'mark': m['mark'], 'notional': m['notional'],
                            'low_dist': m['low_dist']})
            return out
        return _chain_lead.detect(
            _contracts('CALL'), _contracts('PUT'), float(close_price),
            strike_min_vol=config.CHAIN_LEADERSHIP_STRIKE_MIN_VOL,
            min_breadth=config.CHAIN_LEADERSHIP_MIN_BREADTH,
            min_combined_vol=config.CHAIN_LEADERSHIP_MIN_COMBINED_VOL,
            min_notional=config.CHAIN_LEADERSHIP_MIN_NOTIONAL,
            leadership_margin=config.CHAIN_LEADERSHIP_MARGIN,
            convexity_min_frac=config.CHAIN_LEADERSHIP_CONVEXITY_FRAC)

    def _chain_leadership_entry(self, symbol, verdict, opt_data_map, vol_deltas, levels,
                                close_price, expiry, bars, bar_time, now, next_day_mode, pc_ratio):
        """
        Build a production signal from a confirmed chain-leadership verdict — trading the
        recommended (convexity) contract. Bypasses the old single-strike chain-led gates
        (leadership is already established by detect()); keeps a contract-not-chased sanity
        check. Returns (signal | None, block_reason | None).
        """
        side = verdict['controlling_side']
        signal_type = 'BULLISH' if side == 'CALL' else 'BEARISH'
        if self._fired_today.get((symbol, signal_type)):
            return None, None
        rec = float(verdict['recommended_strike'])
        key = (rec, side)
        if key not in opt_data_map:
            return None, 'LEADERSHIP_STRIKE_NOT_QUOTED'
        quote = opt_data_map[key]
        delta = vol_deltas.get(key, 0)
        m = self._strike_metrics(symbol, key, quote, delta, bar_time, expiry)
        # Momentum entry: no value "near-low" ratio gate (the chain already moved) — only a
        # premium floor so we never chase into a worthless penny.
        if (m['mark'] or 0) < config.CHAIN_LEADERSHIP_MIN_PREMIUM:
            return None, 'LEADERSHIP_PREMIUM_TOO_LOW'
        ev = self._eval_volume(symbol, list(self._opt_vol_hist.get((symbol, rec, side), [])),
                               delta, m['low_dist'], 300, 300, 3.0,
                               config.STAIRSTEP_EXCITATION_MIN, mark=m['mark'],
                               is_atm=True, next_day_mode=next_day_mode)
        day_mode = 'NEXT_DAY' if next_day_mode else '0DTE'
        loc_type = 'SUPPORT' if side == 'CALL' else 'RESISTANCE'
        conf01 = round(verdict['confidence'] / 100.0, 3)
        sig = self._build_signal(
            symbol, {'strike': rec}, levels, 0, 'CHAIN_LEADER', loc_type,
            side, signal_type, {'close': close_price}, expiry, quote, ev,
            delta, m['low_dist'], ev, delta, True,
            conf01, 'CHAIN_LEADERSHIP', None, pc_ratio,
            _pc_conviction(signal_type, pc_ratio), next_day_mode, day_mode, quote,
            rec, None)
        sig['signal_context'] = 'CHAIN_LEADERSHIP_ENTRY'
        sig['signal_shape'] = sig['flow_shape'] = 'CHAIN_LEADERSHIP'
        sig['level_label'] = 'CHAIN_LEADER'
        sig['bias'] = 'Chain leadership call' if side == 'CALL' else 'Chain leadership put'
        sig['chain_strikes'] = verdict['supporting_strikes']
        sig['chain_combined_3m'] = verdict['combined_volume']
        sig['leadership'] = {
            'controlling_side': side, 'leader_strike': verdict['leader_strike'],
            'recommended_strike': rec, 'supporting_strikes': verdict['supporting_strikes'],
            'breadth': verdict['breadth'], 'combined_notional': verdict['combined_notional'],
            'confidence': verdict['confidence'],
        }
        return sig, None

    def _countertrend_gate(self, symbol, signal_type, atm, itm, leadership, bar_time):
        """
        §8-12 — verdict for a candidate vs the active intraday trend:
          'CONTINUATION' — no active trend or aligned → ordinary signal,
          'REVERSAL'     — opposes a still-working, leadership-confirmed move AND clears
                           the stricter floors / chain / leadership / thesis gate → fire,
          'WATCH'        — opposes but fails the stricter gate → hold, do not fire.
        """
        trend_dir = self._trend.active_direction(symbol)
        if trend_dir is None or signal_type == trend_dir:
            return 'CONTINUATION', None

        def grp(d):
            return d.get(symbol, d['default'])
        ct_s, ct_3, ct_5 = (grp(config.COUNTERTREND_SINGLE_PRINT_FLOOR),
                            grp(config.COUNTERTREND_3M_FLOOR), grp(config.COUNTERTREND_5M_FLOOR))
        peak1m = atm.get('peak_1m') or 0
        vol3m  = atm.get('vol_3m') or 0
        vol5m  = atm.get('vol_5m') or 0
        vol_ok   = peak1m >= ct_s or vol3m >= ct_3 or vol5m >= ct_5                    # §9
        chain_ok = (bool(atm.get('valid')) and bool(itm.get('valid'))) or peak1m >= 1.5 * ct_s  # §10
        call_ld = (leadership or {}).get('call_leadership', 0.0) or 0.0
        put_ld  = (leadership or {}).get('put_leadership', 0.0) or 0.0
        opp, same = (put_ld, call_ld) if signal_type == 'BEARISH' else (call_ld, put_ld)
        lead_ok = (opp >= config.COUNTERTREND_LEADERSHIP_MIN
                   and (opp - same) >= config.COUNTERTREND_LEADERSHIP_MARGIN)          # §11
        thesis_ok = (self._trend.same_side_fading(symbol, trend_dir, bar_time)
                     or not self._trend.still_working(symbol))                         # §12
        if vol_ok and chain_ok and lead_ok and thesis_ok:
            return 'REVERSAL', None
        if not vol_ok:
            reason = 'COUNTERTREND_VOLUME_INSUFFICIENT'
        elif not chain_ok:
            reason = 'COUNTERTREND_CHAIN_CONFIRMATION_MISSING'
        elif not lead_ok:
            reason = 'COUNTERTREND_LEADERSHIP_INSUFFICIENT'
        else:
            reason = 'ACTIVE_TREND_NOT_FADING'
        return 'WATCH', reason

    def _note_countertrend_watch(self, symbol, signal_type, label, signal, bar_time):
        """§14 — record a countertrend watch once (in-memory, 30-min window). The per-poll
        re-evaluation promotes it to a REVERSAL automatically when conviction appears; the
        watch itself never fires a Discord alert nor consumes the day's direction allowance."""
        key = (symbol, signal_type)
        w = self._countertrend_watch.get(key)
        if w and (bar_time - w['start']).total_seconds() / 60.0 > config.COUNTERTREND_WATCH_MINUTES:
            w = None
        if w is None:
            self._countertrend_watch[key] = {
                'start': bar_time, 'direction': signal_type, 'level': label,
                'initial_volume': signal.get('atm_vol_1m'),
                'initial_ratio': signal.get('atm_spike_ratio'),
                'initial_notional': signal.get('premium_notional'),
            }
            logger.info("COUNTERTREND_WATCH_CREATED  %s %s @ %s  vol=%s ratio=%s",
                        symbol, signal_type, label, signal.get('atm_vol_1m'),
                        signal.get('atm_spike_ratio'))

    def _log_eval(self, symbol, label, strike, spot, confirm_type, *,
                  reason: str, dist: float, atm: dict = None,
                  low: Optional[float] = None, hv: Optional[float] = None) -> None:
        """§21 per-level evaluation log with the blocked reason (+ §73 candidate record)."""
        a = atm or {}
        logger.info(
            "MONITOR  %s %s@%.2f  spot=%.2f  dist=%.3f%%  %s  "
            "1m=%d(x%.1f) win=%d(x%.1f,act=%d) exc=%.2f  low=%s  hv=%s  → %s",
            symbol, label, strike, spot, dist * 100, confirm_type,
            a.get('delta', 0), a.get('spike_ratio', 0.0),
            a.get('vol', 0), a.get('ratio', 0.0), a.get('active', 0), a.get('excitation', 0.0),
            f"{low:.2f}" if low is not None else "n/a",
            f"{hv:.2f}" if hv is not None else "n/a",
            reason,
        )
        self._record_candidate(symbol, label, strike, spot, confirm_type,
                               reason=reason, dist=dist, atm=atm, low=low, hv=hv)

    def _record_candidate(self, symbol, label, strike, spot, confirm_type, *,
                          reason, dist, atm=None, low=None, hv=None) -> None:
        """§73 — append one candidate-evaluation record (blocked OR passed) for the caller
        to persist to signal_candidates. Booleans are derived from the blocked reason."""
        a = atm or {}
        base = reason.split(':', 1)[0]                       # strip the granular suffix
        near = base != 'NOT_NEAR_LEVEL'
        self.last_candidates.append({
            'symbol': symbol, 'candidate_side': confirm_type, 'level_label': label,
            'strike': float(strike), 'spot': round(float(spot), 4),
            'dist_pct': round(dist * 100, 4), 'near_level': near,
            'contract_low_distance': (round(low, 4) if low is not None else None),
            'contract_near_low': (low is None or low <= config.NEAR_LOW_MAX_DIST),
            'valid_volume_event': not base.startswith('NO_VALID_VOLUME') and near,
            'already_alerted': base == 'ALREADY_ALERTED_TODAY',
            'alert_fired': False,
            'signal_type': 'BULLISH' if confirm_type == 'CALL' else 'BEARISH',
            'blocked_reason': reason[:48],
            'hv_pctile': hv,
            'atm_vol_1m': a.get('delta'), 'win_vol': a.get('vol'),
            'active_bars': a.get('active'),
            # ── Production two-path gate fields (§12/§15 research logging) ──
            'gate_path':        a.get('path'),
            'gold_standard':    bool(a.get('gold_standard')),
            'pending':          bool(a.get('pending')),
            'trigger_volume':   a.get('trigger_volume'),
            'trigger_ratio':    a.get('trigger_ratio'),
            'premium_notional': a.get('premium_notional'),
            'peak_1m':          a.get('peak_1m'),
            'vol_3m':           a.get('vol_3m'),
            'vol_5m':           a.get('vol_5m'),
            'event_share':      a.get('event_share'),
            'persistent_bg':    bool(a.get('persistent_bg')) if a else None,
            'bar_status':       a.get('bar_status'),
            'observed_vol':     a.get('observed_vol'),
            'completed_vol':    a.get('completed_vol'),
            'classification':   ','.join(a.get('classification') or []) or None,
        })

    def _build_signal(
        self,
        symbol: str,
        level,
        levels: list,
        rank: int,
        level_label: str,
        level_type: str,
        confirm_type: str,
        signal_type: str,
        current_bar: dict,
        expiry: Optional[date],
        atm_data: dict,
        atm: dict,
        atm_delta: int,
        atm_low_dist: Optional[float],
        itm: dict,
        itm_delta: int,
        atm_itm_confirm: bool,
        cluster_strength: float,
        signal_shape: str,
        hv_pctile: Optional[float],
        pc_ratio: Optional[float],
        pc_conviction: str,
        next_day_mode: bool,
        day_mode: str,
        trade_data: dict,
        traded_strike: float,
        target_level: Optional[float],
    ) -> dict:
        """Build the signal dict (DB/Sheets/exits compatible). confidence = 'HIGH'."""
        now    = datetime.now(CST)
        strike = float(level['strike'])
        spot   = current_bar['close']

        # Exit targets — shifted one level out (skip the too-close nearest level).
        # Roles are frozen (no flipping), so use the frozen SUPPORT/RESISTANCE types.
        exit1_price, exit2_price = compute_exit_targets(
            signal_type, spot, levels, position_only=False, origin_level=strike)

        bias = 'Call-side bias' if level_type == 'SUPPORT' else 'Put-side bias'

        opt_mark = trade_data.get('mark')
        opt_bid  = trade_data.get('bid')
        opt_ask  = trade_data.get('ask')
        # §1/§13 realistic executable fill at commit (near the ask) + method label.
        price_to_enter, paper_fill_method = _executable_fill(opt_bid, opt_ask, opt_mark)
        price_to_exit  = round(price_to_enter * 2, 2) if price_to_enter else None

        option_hl_flag: Optional[str] = None
        day_high = trade_data.get('day_high')
        day_low  = trade_data.get('day_low')
        mark     = opt_mark or 0
        if mark > 0:
            if day_high and day_high > 0:
                if mark >= day_high * 0.97:
                    option_hl_flag = 'AT_HIGH'
                elif mark >= day_high * 0.90:
                    option_hl_flag = 'NEAR_HIGH'
            if option_hl_flag is None and day_low and day_low > 0:
                if mark <= day_low * 1.03:
                    option_hl_flag = 'AT_LOW'
                elif mark <= day_low * 1.10:
                    option_hl_flag = 'NEAR_LOW'

        signal = {
            'symbol':           symbol,
            'signal_time':      now,
            'signal_type':      signal_type,
            'bias':             bias,
            'level_type':       level_type,
            'level_price':      strike,
            'level_label':      level_label,      # 'R1'…'S3' (used in Discord/selection)
            'level_rank':       rank,
            # §1 production signal context (chain-led / countertrend override this)
            'signal_context':       'PRIMARY_LEVEL_CONTINUATION',
            'emergent_location_id': None,
            'target1_oi_name':      None,
            'target2_oi_name':      None,
            'expiry':           expiry,
            'trigger_price':    spot,
            'option_type':      confirm_type,
            'day_mode':         day_mode,
            'traded_strike':    traded_strike,
            'target_level':     target_level,
            'opt_mark':         opt_mark,
            'opt_bid':          opt_bid,
            'opt_ask':          opt_ask,
            'price_to_enter':   price_to_enter,
            'price_to_exit':    price_to_exit,
            'paper_fill_method': paper_fill_method,
            'prox_score':       1.0,              # binary near-level → always 1.0
            'cluster_strength': cluster_strength,
            'strong_cluster':   atm_itm_confirm,
            'flow_shape':       signal_shape,
            'signal_shape':     signal_shape,
            'confidence':       'HIGH',
            'upgrade':          False,
            'cluster_active_bars': atm.get('active', 0),
            'cluster_burst_bars':  atm.get('burst', 0),
            'atm_vol_1m':       atm_delta,
            'atm_spike_ratio':  atm.get('spike_ratio', 0.0),
            'atm_vol_3m':       atm.get('vol', 0),
            # Volume trigger (what Discord shows: single-bar vs multi-min window)
            'trigger_volume_type': atm.get('trigger_type'),
            'trigger_volume':      atm.get('trigger_volume'),
            'trigger_ratio':       atm.get('trigger_ratio'),
            # §13 production-gate display fields
            'gate_path':        atm.get('path'),
            'gold_standard':    bool(atm.get('gold_standard')),
            'observed_vol':     atm.get('observed_vol'),
            'completed_vol':    atm.get('completed_vol'),
            'bar_status':       atm.get('bar_status'),
            'peak_1m':          atm.get('peak_1m'),
            'vol_3m_window':    atm.get('vol_3m'),
            'event_share':      atm.get('event_share'),
            'premium_notional': atm.get('premium_notional'),
            'itm_vol_1m':       itm_delta,
            'itm_spike_ratio':  itm.get('spike_ratio', 0.0),
            'itm_vol_3m':       itm.get('vol', 0),
            'spread_pct':       (round(_spread_pct(trade_data), 4)
                                 if _spread_pct(trade_data) is not None else None),
            'low_dist':         atm_low_dist,
            'room_score':       None,             # §1 target-room removed as a gate
            'room_pct':         None,
            'hv_pctile':        hv_pctile,
            'pc_ratio':         pc_ratio,
            'pc_conviction':    pc_conviction,
            'option_hl_flag':   option_hl_flag,
            'exit1_price':      exit1_price,
            'exit2_price':      exit2_price,
            # Legacy nullable columns expected by db.save_signal
            'opt_vol_delta':      atm_delta,
            'avg_volume_20':      None,
            'spike_volume':       None,
            'consecutive_spikes': None,
        }

        # P-ET: attach the contract's frozen event-time state (for persistence + later
        # event-time eligibility). None when disabled or no event was registered.
        if config.EVENT_TIME_ELIGIBILITY_ENABLED:
            signal['event_state'] = self._event_reg.get(symbol, traded_strike, confirm_type)

        strike_note = (f"  [{day_mode} strike={traded_strike:.2f}→tgt {target_level:.2f}]"
                       if next_day_mode and target_level is not None else "")
        logger.info(
            "SIGNAL >> %s %s (%s)  %s@%.2f%s  %s:1m=%d(x%.1f) win=%d(x%.1f,act=%d) exc=%.2f  "
            "low=%.2f  hv=%s  shape=%s  mark=%s  enter=%s",
            symbol, signal_type, bias, level_label, strike, strike_note,
            confirm_type, atm_delta, atm.get('spike_ratio', 0.0),
            atm.get('vol', 0), atm.get('ratio', 0.0), atm.get('active', 0),
            atm.get('excitation', 0.0),
            atm_low_dist if atm_low_dist is not None else 0.0,
            f"{hv_pctile:.2f}" if hv_pctile is not None else 'n/a',
            signal_shape,
            f"${opt_mark:.2f}"       if opt_mark      else 'n/a',
            f"${price_to_enter:.2f}" if price_to_enter else 'n/a',
        )
        return signal

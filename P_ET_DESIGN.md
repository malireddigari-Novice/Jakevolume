# P-ET — Event-Time Capture: implementation design (pre-draft)

Status: **DESIGN ONLY — no code written.** Prereq phase before P3. Everything lands
behind the Gold flags (dormant until enabled); no change to live behavior when off.

Goal: preserve market state at the instant volume crosses threshold, and judge strike
eligibility by **event-time** ATM/distance rather than **evaluation-time** — fixing the
TSLA-425P failure (ATM at the opening flow, deep ITM before the 1-min bar finishes).

---

## 0. Data-availability constraints (must design around these)

- **Poll cadence = `POLL_INTERVAL_SECONDS` (60s).** Alpaca snapshots expose **cumulative
  day volume**; the detector already derives per-minute deltas (`cur_vol - prev_vol`).
  → True sub-60s rolling is NOT available from Alpaca at 60s polling.
- **Databento path** subscribes to live OPRA trades (`data/databento_client.py`), so
  sub-minute rolling IS possible there.
- **Subscription is already ±10% of spot** (`get_watched_contracts`, lo/hi = spot*0.90/1.10)
  — wider than ATM±5. §2 "opening window" is therefore a **detector-evaluation** change,
  not a data-subscription change.

**Decision:** implement rolling as an INTERFACE (`RollingVolume`) whose default backend
maps `rolling_60s → latest 1-min delta`, `rolling_180s → sum(last 3 deltas)`; a Databento
trade-stream backend can later provide true seconds. The production floor stays the same
(`60s>=1000 OR 180s>=2000`), so no numeric behavior change vs today's `peak_1m/vol_3m`
until a finer feed is wired. Document the coarse granularity honestly (no silent claim of
sub-minute precision).

---

## 1. `RollingVolume` tracker interface

`analysis/rolling_volume.py` (new), one instance per contract, held in the detector.

```
class RollingVolume:
    def observe(self, ts, cumulative_or_delta) -> None      # ingest a sample
    def r60(self) -> int                                     # rolling 60s
    def r180(self) -> int                                    # rolling 180s
    def peak_1m(self) -> int
    # backends: BarDeltaBackend (default, 1-min) | TradeStreamBackend (Databento)
```

Detector holds `self._rvol: dict[_OptKey, RollingVolume]`. Fed in check() Step 1 (below).
Default backend just wraps the existing `self._opt_vol_hist[key]` deltas → r60 = deltas[-1],
r180 = sum(deltas[-3:]). No new numeric behavior; it's the seam for a finer feed later.

---

## 2. Event registry + `EventState` (the capture layer)

Detector-held: `self._event_state: dict[_OptKey, EventState]`.

```
@dataclass
class EventState:
    event_start_time            # first watch-threshold cross for this contract
    threshold_cross_time        # first PRODUCTION-floor cross
    spot_at_event_start
    spot_at_threshold_cross
    atm_strike_at_event_start   # frozen ATM at event start
    atm_strike_at_threshold_cross
    strike_distance_from_atm_at_event      # |contract_strike - atm_at_event| in strikes
    bid_at_threshold, ask_at_threshold, mid_at_threshold, last_at_threshold
    r60_at_threshold, r180_at_threshold
    completed_1m_volume, revised_1m_volume
    observed_volume_at_decision, threshold_cross_volume, final_revised_volume   # §5
    decision_timestamp, revision_timestamp                                      # §5
    ttl_expires_at              # OPENING_EVENT_CONTRACT_TTL_MIN from event_start
```

Lifecycle (per contract, each poll):
1. **watch cross** — first time `r60 >= OPENING_EVENT_WATCH_VOLUME (500)` (or base watch):
   create EventState, snapshot spot + **compute ATM once and freeze it**, set TTL.
2. **threshold cross** — first time `r60 >= 1000 OR r180 >= 2000`: stamp
   `threshold_cross_time`, quotes-at-threshold, r60/r180, `strike_distance_from_atm_at_event`
   (from the FROZEN atm), `observed_volume_at_decision`, `decision_timestamp`.
3. **stays alive until `ttl_expires_at`** even if the contract goes ITM/OTM (§3) — this is
   what keeps TSLA-425P eligible after spot runs away.
4. **prune** on TTL or new day.

Eligibility (§1/§14): the ATM/1-ITM classification uses
`strike_distance_from_atm_at_event`, NOT the evaluation-time distance. Both are stored;
`strike_distance_from_atm_at_evaluation` is kept for audit only.

---

## 3. `signal_event_state` schema (db/schema.sql)

```
CREATE TABLE IF NOT EXISTS signal_event_state (
    signal_id BIGINT PRIMARY KEY REFERENCES signals(id),
    symbol VARCHAR(10), contract_strike NUMERIC(12,4), option_type VARCHAR(4),
    event_start_time TIMESTAMPTZ, threshold_cross_time TIMESTAMPTZ,
    spot_at_event_start NUMERIC(12,4), spot_at_threshold_cross NUMERIC(12,4),
    atm_strike_at_event_start NUMERIC(12,4), atm_strike_at_threshold_cross NUMERIC(12,4),
    strike_distance_from_atm_at_event SMALLINT,
    strike_distance_from_atm_at_evaluation SMALLINT,
    bid_at_threshold NUMERIC(12,4), ask_at_threshold NUMERIC(12,4),
    mid_at_threshold NUMERIC(12,4), last_at_threshold NUMERIC(12,4),
    rolling_60s_volume BIGINT, rolling_180s_volume BIGINT,
    completed_1m_volume BIGINT, revised_1m_volume BIGINT,
    observed_volume_at_decision BIGINT, threshold_cross_volume BIGINT,
    final_revised_volume BIGINT,
    decision_timestamp TIMESTAMPTZ, revision_timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```
`db.save_signal_event_state(signal_id, EventState)` called right after `save_signal`.

---

## 4. Exact capture points in `analysis/signal_detector.py`

- **Step 1 per-contract loop (~361-389):** after updating `_opt_vol_hist`, feed
  `self._rvol[key].observe(...)` and run the EventState lifecycle (watch/threshold/prune).
  This is the ONLY place that must run for every subscribed contract each poll — so the
  registry sees ATM±window contracts, not just level contracts.
- **ATM selection (~434, `atm_key = min(... abs(k[0]-close_price))`):** unchanged for the
  live spot, but eligibility of a candidate contract now also consults
  `EventState.strike_distance_from_atm_at_event`. A contract that is >window strikes away at
  evaluation but was ATM/1-ITM at event stays eligible.
- **Production gate `_eval_volume` (~610):** accept `r60/r180` from the tracker (already
  equivalent to peak_1m/vol_3m by default). Add the §5 partial→revised handling:
  observed < floor at decision → `SUBTHRESHOLD_PARTIAL_EVENT`; if a later poll's revised bar
  crosses the floor, treat as a FRESH decision (new `decision_timestamp`, current quote/spot/
  ATM) → `REVISED_BAR_THRESHOLD_CROSSED`, never backdated.
- **`_build_signal` (~1370):** attach the frozen `EventState` to the signal dict so main.py
  can `save_signal_event_state` after `save_signal`.

---

## 5. §2/§3 opening universe (detector-side)

- Opening 15 min (`is_opening_range()`): the candidate builder evaluates **ATM ± 5 strikes**
  (not only S/R level contracts). Data already provides ±10%, so this is a widening of the
  detector's per-poll evaluation set + registering watch events for all of them.
- Registry TTL (`OPENING_EVENT_CONTRACT_TTL_MIN=30`) keeps opening events alive past the move.

---

## 6. Config additions (all env-overridable; no behavior change until used)

```
OPENING_STRIKE_WINDOW          = 5      # ATM ± N strikes during opening
OPENING_EVENT_WATCH_VOLUME     = 500
OPENING_EVENT_CONTRACT_TTL_MIN = 30
EVENT_TIME_ELIGIBILITY_ENABLED = false # master gate for the event-time distance rule
ROLLING_VOLUME_BACKEND         = 'bar'  # 'bar' | 'trade_stream'
```

---

## 7. Build order & tests (within P-ET)

1. `RollingVolume` + `BarDeltaBackend` + unit test (r60/r180 == existing peak_1m/vol_3m).
2. `EventState` + registry lifecycle in the detector (feature-flagged) + unit test
   (watch→threshold→TTL; frozen ATM; event-time distance).
3. `signal_event_state` schema + `save_signal_event_state` + wire in main.py.
4. §5 no-retro partial→revised flow + labels + test (TSLA-425P@501 → SUBTHRESHOLD;
   later revise → fresh decision, not backdated).
5. Opening ATM±5 evaluation + TTL registry.
6. Regression: the default (`EVENT_TIME_ELIGIBILITY_ENABLED=false`, bar backend) must be
   byte-identical to today. Control: TSLA-425P stays eligible after moving ITM.

**Unblocks:** P3 Route B (§8) + opening directional story (§9) — both need the frozen
event-time ATM and the opening registry that P-ET provides.
```

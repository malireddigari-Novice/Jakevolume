# Gold-Only Production Mode — Implementation Plan

Status: **PROPOSED — awaiting approval. No production code written yet.**
Guardrail: everything ships behind `GOLD_ONLY_PRODUCTION_MODE = false`. Today's live
session is unaffected until you review, the §26 control tests pass, and you flip it on.

---

## 0. Guiding architecture

The spec is largely a **classification + gating layer** on top of the existing
candidate→signal pipeline in `analysis/signal_detector.py`, plus a few genuinely new
subsystems. We do NOT rewrite the detector. We add:

- `analysis/gold_mode.py` (NEW) — classify a passing candidate into a Gold subtype
  (§2/§3/§4), run the final `ProductionEntryAllowed` gate (§18), and route everything
  else to research-only (§19). This is the single production chokepoint.
- `analysis/intent_validation.py` (NEW) — §5–§9 directional-intent + opposite-side veto.
- `analysis/opening_scan.py` (NEW) — §10–§11 opening full-chain scan + story.
- Extend, don't replace: `signal_detector.check()` routes each passing candidate through
  `gold_mode`. The existing gates (proximity, two-path conviction, contract-low,
  historical-value, short-cover, leadership, dedup) become **inputs** to Gold scoring.

The existing `_log_eval(...)` candidate rail already stores every evaluated candidate with
a `blocked_reason`; that IS the research-only bus (§19). We extend it with Gold fields.

**Deliberate tradeoff (call-out):** §6 intent validation uses the event bar + the next
1–3 completed 1-min bars, so a Gold alert fires **after** confirmation — up to ~1–3 min
later than today's fire-on-event-bar. This is precision-over-speed by design, and is
compatible with §16 (which only forbids delaying for Sheets/reports/serial symbols, not
for intent confirmation). Non-intent Gold paths (e.g. a clean primary level with immediate
strong response) can confirm on 1 bar.

---

## 1. Config additions (§25) — `config.py`

All default to the safe/off value so nothing changes until enabled:

```
GOLD_ONLY_PRODUCTION_MODE          = false   # master switch
GOLD_PRIMARY_ENABLED               = true    # (only matters when master on)
GOLD_CHAIN_LED_ENABLED             = true
PRIMARY_CHAIN_MERGE_ENABLED        = true
INTENT_VALIDATION_ENABLED          = true
INTENT_CONFIRMATION_BARS_MIN       = 1
INTENT_CONFIRMATION_BARS_MAX       = 3
OPPOSITE_SIDE_VETO_ENABLED         = true
OPENING_FULL_CHAIN_SCAN_ENABLED    = true
HISTORICAL_VALUE_REGION_MODEL_ENABLED = true
SAME_DIRECTION_UPGRADE_ENABLED     = true
MAX_SAME_DIRECTION_UPGRADES_PER_DAY= 1
COUNTERTREND_STRICT_MODE           = true
ESTABLISHED_MOVE_PCT               = 0.01
LEADERSHIP_FADE_RATIO              = 0.50
FRESH_CONVICTION_LOOKBACK_MIN      = 10
TREND_PROGRESS_LOOKBACK_BARS       = 5
# value-region + contract-low-region thresholds (§12/§13)
HV_REGION_EXCELLENT_MAX = 0.25
HV_REGION_ACCEPTABLE_MAX= 0.45
HV_REGION_NEUTRAL_MAX   = 0.65
CLOW_GOLD_MAX      = 1.25
CLOW_STRONG_MAX    = 1.50
CLOW_ACCEPTABLE_MAX= 1.75
# premium-notional floor (§2E) + intent response tolerances
GOLD_MIN_PREMIUM_NOTIONAL = <tune>
INTENT_PREMIUM_HOLD_PCT   = <tune>   # e.g. mark must hold >= -X% over confirm bars
```

## 2. Schema additions — `db/schema.sql` (all `ADD COLUMN IF NOT EXISTS`)

- Widen `signals.signal_context` to VARCHAR(48) to hold Gold subtypes
  (`GOLD_PRIMARY_LEVEL`, `GOLD_CHAIN_LED_CALL/PUT`, `PRIMARY_AND_CHAIN_CONFIRMED`,
  `HIGH_CONVICTION_SAME_DIRECTION_UPGRADE`, `CONFIRMED_COUNTERTREND_REVERSAL`).
- `signals`: `gold_grade VARCHAR(12)` (GOLD/RESEARCH), `intent_class VARCHAR(40)` (§8),
  `opp_veto VARCHAR(48)`, `value_region VARCHAR(28)` (§12), `clow_region VARCHAR(32)` (§13),
  `premium_notional NUMERIC`, `delta_notional NUMERIC`, `call_leadership NUMERIC`,
  `put_leadership NUMERIC`.
- NEW table `signal_latency` (§17): one row per production alert with the 11 timestamps
  and 6 derived latencies.
- NEW table `opening_chain_scan` (§10): per opening-window evaluation snapshot (the 16
  stored fields) + story classification (§11).
- NEW table `intent_validation` (§5–§8): event → confirmation-bar observations + verdict.

## 3. Section → implementation map

| § | What | Where | Status |
|---|------|-------|--------|
| 1,18,19 | Gold-only gate + research-only routing | `gold_mode.classify()` + `ProductionEntryAllowed`; `_log_eval` rail | NEW gate over EXISTING rail |
| 2A | Location (spot near primary S/R) | proximity gate `dist <= near_thr` | EXISTS |
| 2B | ATM / 1-ITM correct side | `atm_key`/`itm_key` selection | EXISTS |
| 2C | Absolute volume (two-path) | `_eval_volume` `valid` | EXISTS |
| 2D,13 | ContractLowDistance regions | `_contract_low_dist` + region grading | EXTEND (graded) |
| 2E | Premium notional floor | new: mark×100×qty vs `GOLD_MIN_PREMIUM_NOTIONAL` | NEW |
| 2F | EventShare/active/persistent | `volume_event()` in flow_reversal | EXISTS (reuse) |
| 2G,5,6,8 | Directional-intent validation | `intent_validation.py` (event + 1–3 bars) | NEW |
| 2H,9 | Opposite-side leadership veto | `intent_validation.opposite_side_veto()` using `compute_leadership_scores` | NEW (on existing leadership) |
| 3 | Gold chain-led | wrap existing `_chain_led_entry` + intent/veto | EXTEND |
| 4 | Primary+Chain merge | `gold_mode.merge()` — dedup to one alert | NEW |
| 7 | Option-supply classification | `intent_validation` verdicts (`PROBABLE_*_SUPPLY`) | NEW (short_cover_risk is a partial seed) |
| 10,11 | Opening full-chain scan + story | `opening_scan.py`, called in opening window | NEW |
| 12 | Historical-value regions | `_historical_value_pctile` + region grading | EXTEND (graded, replaces binary max) |
| 14 | Same-direction upgrade | `gold_mode` + `_fired_today` upgrade slot (max 1/day) | EXTEND (builds on the dedup I just fixed) |
| 15 | Countertrend strict | existing countertrend gate + `self._trend` + strict thresholds | EXTEND |
| 16 | Completed-bar / PENDING_VOLUME_CONFIRMATION | `_completed_bar` exists; add pending state | EXTEND |
| 17 | Latency logging | timestamps through check()→commit→discord; `signal_latency` table | NEW |
| 20 | Target integrity | `compute_exit_targets` (price-ordered) — audit + assert | VERIFY/ENFORCE |
| 21–24 | Metrics / daily report / false-signal + missed-move reviews | `daily_review.py`, `nightly_pipeline.py` | EXTEND |
| 25 | Config | `config.py` | NEW |
| 26 | Control tests A–E | `test_gold_mode.py` | NEW |

## 4. Phased delivery (each phase = one reviewable commit + its tests; master flag stays OFF)

- **P1 — Foundation & gate.** §25 config, §2 schema, `gold_mode.py` skeleton with the
  §18 `ProductionEntryAllowed` gate + §1/§19 routing, §12/§13 value-region grading,
  §20 target-integrity assertion, Discord Gold card + §4 merge. Wired but gated OFF.
  Validates: gate routes non-Gold → research-only; existing tests still green.
- **P2 — Intent & veto (§5–§9).** `intent_validation.py`: deferred confirmation over
  1–3 bars, supply classification (§7), opposite-side veto (§9). Control **TEST A, E**.
- **P3 — Opening scan (§10–§11).** `opening_scan.py` + story classification. **TEST B**.
- **P4 — Upgrade & countertrend (§14–§15).** Same-direction upgrade slot, countertrend
  strict. Confirms reversals still fire (they're exempt).
- **P5 — Latency & analytics (§16–§17, §21–§24).** Latency instrumentation + table,
  expanded daily report, Claude false-signal/missed-move review prompts.
- **P6 — Control tests green (§26 A–E) + validation sign-off.** Full offline replay;
  only then do you set `GOLD_ONLY_PRODUCTION_MODE=true`.

## 5. Open questions / data dependencies to confirm before P2

1. **Intraday IV availability.** §6/§7 want call/put IV change over confirm bars. The
   Schwab chain carries `implied_vol`, but do the intraday `option_quotes` include it? If
   not, intent validation leans on premium (mark/bid) + leadership + spot response and
   treats IV as optional-when-present. **Need to verify the intraday quote source fields.**
2. **Premium-notional & delta-notional floors** (§2E, §9) need concrete numbers — propose
   deriving from a backtest percentile, or you set them.
3. **Deferred-alert latency.** Confirm the 1–3 bar intent wait (adds ≤3 min) is acceptable
   for 0DTE given the precision goal — it is the core speed/precision tradeoff.
4. Same-direction upgrade (§14) interacts with the one-per-side dedup I just shipped; the
   upgrade becomes a single sanctioned exception (max 1/day), gated on strictly-better
   volume+value+intent.

## 6. What this does NOT change
- Reversals (`flow_reversal` → `send_reversal_alert`) stay exempt from the Gold gate.
- The morning briefing, weekend-gap, open-position, and proximity-level work already
  shipped are untouched.
- With the master flag OFF, production behavior is identical to today.

---

# Addendum — July 1 case-study patch (folded into the phases above)

The July-1 audit (NVDA 195C, MSFT 380C/385P/387.5P, TSLA 425P/425C/430P) is the same
Gold effort, deepened with concrete cases. Mapping:

**Already shipped (live):**
- Revised volume floors — `peak_1m>=1000 OR vol_3m>=2000`, opening 1250/2500 (commit a4f381d).
- Target integrity — origin level can never be a target (commit 89605f3); §10/§20.
- One-per-side dedup (commit dcde3bf) underpins §8 same-direction handling.

**New/refined requirements folded into phases:**
- **Exceptional single-strike route (Route B)** — `peak_1m>=2000` substitutes for adjacent
  confirmation when ≤2 strikes from ATM + notional + value + activation + no opposite
  dominance. → P3 (chain-led). *Additive path — must be flag-gated + tested (would have
  caught MSFT 380C / TSLA 425C).*
- **No retrospective threshold qualification** — production eligibility is decided from
  info available at the decision timestamp; a bar that revises above the floor later is a
  FRESH decision at the CURRENT price/spot/ATM, never backdated to the earlier cheap fill.
  Labels: `SUBTHRESHOLD_PARTIAL_EVENT` → `REVISED_BAR_THRESHOLD_CROSSED`. → P5 (with §17
  latency + price-source audit). *TSLA 425P case.*
- **Alert-price / paper-fill synchronization** — store event-bar OHLC + bid/ask/mid/last at
  threshold-cross AND at signal-commit; paper fill = realistic executable near the ask at
  commit time; flag any price outside the event bar's range. → P5. *NVDA $1.20-vs-$0.89–1.03.*
- **Flow DETECTED vs ACTIVATED** — detection (exceptional concentrated volume) is separate
  from activation (premium/IV hold-or-expand + underlying responds + leadership grows).
  Production requires ACTIVATED; countertrend requires stricter activation. This IS the §5–§9
  intent-validation subsystem. → P2. *MSFT 385P 12:24 = detected-not-activated.*
- **Near-target exits** — exit runner when within `max($0.50, target*0.0025)` of target AND
  opposite concentrated flow AND leadership fades/price rejects. → P4. *NVDA ~200 runner.*
- **Fresh ATM per event + superior later-event recognition** — recompute ATM every event;
  never reuse the earlier strike; a later independent, better-timed, current-strike event is
  `IMPROVED_TIMING_AND_STRIKE_OPPORTUNITY` / same-dir upgrade; a post-extension opposite
  event is `CONFIRMED_INTRADAY_REVERSAL_PUT`. → P4. *MSFT 385P→387.5P, TSLA 425P→430P.*
- **MAE-before-MFE scoring** — daily report must show MAE-before-MFE and penalize severe
  early drawdown, not credit eventual peak. `signal_outcomes` already has mfe/mae +
  time_to_mfe/time_to_mae to derive ordering. → P5 (analytics).
- **Developer audit output (§13)** — per studied event, the exact gate-by-gate decision
  chain (subscribed? spot? ATM? distance; observed/threshold/completed/revised vol; notional;
  EventShare; value region; low-dist; leadership; premium/IV/underlying response; exact gate
  + rejection; completed-bar reeval?; blocked-by-earlier?; primary-proximity required?; price
  sources). → P5 diagnostic tool.

**Acceptance suite — the 8 July-1 control tests become the P6 gate (in `test_gold_mode.py`):**
1 NVDA195C→GOLD_PRIMARY_LEVEL_CALL · 2 MSFT380C→GOLD_CHAIN_LED/EXCEPTIONAL_SINGLE_STRIKE
(no primary-proximity req) · 3 MSFT385P@12:24→COUNTERTREND_PUT_WATCH (not entry) ·
4 MSFT387.5P@15:11→IMPROVED_TIMING_AND_STRIKE_OPPORTUNITY · 5 TSLA425P@501→SUBTHRESHOLD_PARTIAL_EVENT
(no alert; no backdate) · 6 TSLA422.5C→CHAIN_LED_CALL_WATCH · 7 TSLA425C@2.46K→GOLD_CHAIN_LED/
EXCEPTIONAL_SINGLE_STRIKE · 8 TSLA430P@2.34K post-extension→CONFIRMED_INTRADAY_REVERSAL_PUT.
NVDA 195C is a REQUIRED positive regression; TSLA 425P a REQUIRED negative.

---

# Addendum 2 — Event-Time ATM / Opening Flow patch → new phase **P-ET** (before P3)

**Why a new phase, and why it comes first.** This patch's core requirement is not a
gate tweak: today the engine computes ATM, strike-distance, and quotes at *bar-close /
poll time*, but the patch demands **freezing them at the instant volume crosses the
threshold** (§1, §2, §3, §14) and tracking **rolling-seconds** volume (§4). This is the
TSLA-425P failure mode: a contract that was ATM at the opening flow but is deep ITM
before the 1-min bar finishes — evaluated at bar-close it can be wrongly excluded.
Event-time capture is a **prerequisite** for correct Route B (§8) and opening-flow
(§2/§3/§9), so it is sequenced **ahead of P3**.

## P-ET deliverables (new)
- **Event-time state capture (§1):** on threshold-cross, snapshot `spot_at_event_start`,
  `spot_at_threshold_cross`, `atm_strike_at_event`, `strike_distance_from_atm_at_event`,
  bid/ask/mid/last at threshold, and `rolling_60s/180s/completed_1m/revised_1m`. Persist
  on the candidate/signal (new `signal_event_state` table or signal columns).
- **Eligibility by event-time distance (§1/§14):** strike eligibility uses
  `strike_distance_from_atm_at_event`, NOT at evaluation. Store `selection_reason`.
- **Rolling-seconds volume (§4):** per-contract rolling 60s/180s trackers (vs today's
  1-min bar deltas); `volume_pass = 60s>=1000 OR 180s>=2000`, floors still binding; fire
  when the rolling threshold is crossed without waiting for bar-close (store the completed
  + revised bar for audit).
- **Opening universe + registry (§2/§3):** subscribe ATM±5 in the first 15 min;
  `OPENING_EVENT_WATCH_VOLUME`/`_TTL` registry that freezes the event-time ATM relationship
  and keeps a contract active after it moves ITM/OTM.
- **No-retrospective-qualification (§5):** eligibility decided at `decision_timestamp`;
  a bar that revises over the floor later is a FRESH decision at the current quote/spot/ATM,
  never backdated. Labels `SUBTHRESHOLD_PARTIAL_EVENT` → `REVISED_BAR_THRESHOLD_CROSSED`.

## Full section → phase map (this patch)
| § | Item | Phase | State today |
|---|------|-------|-------------|
| 1 event-time state | **P-ET** | not built |
| 2 opening ATM±5 window | **P-ET** | not built |
| 3 opening event registry | **P-ET** | not built |
| 4 rolling 60/180s (floors live) | **P-ET** | floors LIVE; rolling-seconds not built |
| 5 no-retro-qualification | **P-ET** | not built |
| 6 two independent paths | done | ✅ live |
| 7 Gold primary definition | P1 + P-ET (event-time ATM) + P2 | P1 live / intent dormant |
| 8 chain-led Route A / **Route B** | RouteA ✅ · **Route B → P3** (needs P-ET) | Route B not built |
| 9 opening directional story | **P3** (needs P-ET) | not built |
| 10 two-sided candidate tracking | **P3** | partial (both sides scanned) |
| 11 trend/countertrend classification | **P4** | partial |
| 12 countertrend-strict Gold | **P4** | flag only |
| 13 flow detected vs activated | **P2** | built, dormant |
| 14 fresh ATM + selection_reason | **P-ET** (event-time) | eval-time only |
| 15 same-direction superior event | **P4** | label only |
| 16 primary+chain merge | P1 | ✅ built |
| 17 Gold ⭐ rule | P1+P2+P-ET | partial |
| 18 TSLA 7/2 audit output | **P5** | not built |
| 19 regression controls | **P6** | 2 of ~9 |
| 20 composite production rule | all | partial |

## Revised phase order
**P1 ✅ → P2 ✅ (dormant) → P-ET (event-time capture) → P3 (Route B + opening story, now
unblocked) → P4 (superior-event + countertrend-strict) → P5 (latency + price-sync + audit
output + MAE-before-MFE) → P6 (all control tests, then enable intent live).**

Live today = P1 structural gate + tightened floors (a small slice). P-ET is the next
build and the highest-leverage one — it fixes the event-time correctness the case
studies keep hitting.

---

# Addendum 3 — Primary-level Breakout/Breakdown + continuation → phase **P-BD**

**Core new capability.** Primary OI levels are not only reversal zones — they also
produce *continuation* when price accepts through them. Today the detector hard-codes
the side from the level type (`signal_detector.check()` ~L417):
`confirm_type = PUT if RESISTANCE else CALL` — i.e. only **support→CALL (bounce)** and
**resistance→PUT (rejection)**. P-BD adds the missing two: **resistance→CALL (breakout)**
and **support→PUT (breakdown)**, gated by an *acceptance* test so it never fires on a
mere touch. This is why the AAPL-310C breakout above R1 would be missed today.

## P-BD deliverables (new)
- **Level-interaction classifier (§1/§4):** per level touch/cross, label BOUNCE /
  REJECTION / BREAKOUT / BREAKDOWN / FALSE_BREAKOUT / FALSE_BREAKDOWN / MIXED. Decision
  tree: CALL@support→bounce, CALL@resistance→breakout, PUT@resistance→rejection,
  PUT@support→breakdown.
- **Acceptance rule (§3):** a breakout/breakdown requires spot to *accept* past the level —
  1 completed 1-min bar close beyond it, OR beyond by `max(BREAKOUT_LEVEL_BUFFER_ABS,
  level*BREAKOUT_LEVEL_BUFFER_PCT)`, OR a reclaim/fail with premium continuing to expand.
  Config: `BREAKOUT_ACCEPTANCE_BARS=1`, `BREAKOUT_LEVEL_BUFFER_PCT=0.001`,
  `BREAKOUT_LEVEL_BUFFER_ABS=0.25`.
- **False-breakout/breakdown block (§10):** cross without activation (premium fades +
  opposite flow + spot falls back) → `FALSE_BREAKOUT_WATCH` / `FALSE_BREAKDOWN_WATCH`, no alert.
- **Breakout/breakdown targets (§12):** extend `compute_exit_targets` — breakout-call
  targets strictly *above* max(spot, breakout level, origin); breakdown-put strictly below.
  (Builds on the origin-level target-integrity fix already shipped.)
- **New subtypes + Discord (§13/§14):** `PRIMARY_LEVEL_BOUNCE_CALL / REJECTION_PUT /
  BREAKOUT_CALL / BREAKDOWN_PUT`, merged `PRIMARY_AND_CHAIN_CONFIRMED_*`, and their
  `GOLD_*` forms in `gold_mode`; card shows Level Type / Level Action / Acceptance /
  Flow State / Intent.
- **Structural context on chain-led events (§6):** tag near_support / near_resistance /
  breaking_above / breaking_below / rejecting / bouncing — WITHOUT making primary
  proximity a requirement for chain-led (§6 stays independent).

## Section → coverage (this patch)
| § | Item | Where | State |
|---|------|-------|-------|
| 1,3,4 breakout/breakdown classification + acceptance | **P-BD** | not built (detector hard-codes side) |
| 2 AAPL 310C case | P-BD test | — |
| 5 primary+chain merge (breakout variants) | P1 merge + **P-BD** labels | partial |
| 6 chain-led independent + structural context | ✅ independent · context **P-BD** | partial |
| 7 volume floors mandatory | ✅ live | done |
| 8 event-time ATM | **P-ET** | not built |
| 9 flow detected vs activated | **P2** | built, dormant |
| 10 false breakout/breakdown | **P-BD** | not built |
| 11 countertrend protection | **P4** | partial |
| 12 breakout/breakdown targets | **P-BD** (extends shipped target-integrity) | not built |
| 13 Gold subtypes | P1 + **P-BD** | partial |
| 14 Discord subtype labels | **P-BD** | partial |
| 15 regression tests (AAPL310C, NVDA195C, TSLA425P/412.5C, MSFT380C/385P) | **P6** | 2 of ~7 |
| 16 final rule | all | partial |

## Sequencing
P-BD **depends on P-ET** (acceptance + event-time ATM) and reads P2 activation, so it
slots **after P-ET, overlapping P3**. Order: P1 ✅ → P2 ✅(dormant) → **P-ET → P-BD /
P3 → P4 → P5 → P6**. Nothing new goes live until its phase is built + the control tests
(incl. AAPL 310C breakout) pass.

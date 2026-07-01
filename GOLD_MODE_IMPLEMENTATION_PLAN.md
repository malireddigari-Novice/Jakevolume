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

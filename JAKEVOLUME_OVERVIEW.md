# JakeVolume — Complete System Overview

**As of 2026-07-19.** This is the authoritative top-to-bottom description of how JakeVolume
works today. For the detailed, section-by-section spec and the dated change history see
[`LOGIC.md`](LOGIC.md); this document is the readable end-to-end companion.

---

## 1. What JakeVolume is

JakeVolume is a **deterministic 0DTE options alerting engine** for the Mag-7 (AAPL, AMZN,
GOOGL, META, MSFT, NVDA, TSLA). It is **not a scanner for "interesting" flow** — it is a
state machine that alerts only when institutional-quality leadership has emerged at a
meaningful price location. If an alert fires, it already represents the highest-conviction
opportunity the engine can identify: there are **no tiers, stars, confidence scores, or
quality badges** on the output. The quality decision lives inside the engine; Discord only
explains *what* fired and *why*.

The core thesis it trades: option volume that concentrates **near a contract's value low**,
at a **primary open-interest level** or an **emergent chain**, with **directional
leadership** confirming, tends to precede convex directional moves. Entries are 0DTE (or the
next expiry on Tue/Thu); exits ride a level ladder to EOD.

---

## 2. Architecture & data sources

| Source | Role | Notes |
|---|---|---|
| **Alpaca** | Intraday market data + execution | SIP full-market stock feed, OPRA options (quotes, greeks, 1-min + hourly + daily option price-history), paper execution. The primary live feed. |
| **Schwab** | Morning OI snapshot only | The only source of live open interest. **Serves no option price-history** (that's why Alpaca is used for bars). Refresh token expires ~weekly → periodic re-auth (`schwab_reauth.py`). |
| **Databento** | Disabled fallback | No API key in prod; the startup traceback about it is benign. |
| **PostgreSQL** | All persistence | Snapshots, levels, bars, candidates, signals, trades, instrumentation. |
| **Google Sheets** | Human-readable trade log | Mirror of signals/trades. |
| **Discord** | Alerts + morning brief + reviews | Webhooks. |

Runtime: a single `main.py` process on a 60-second loop, launched at **08:10 CST** by a
Windows Task Scheduler job (`Jakevolume_DailyRun`, `IgnoreNew` single-instance) via
`run_scheduled.bat`, which is a **watchdog** that restarts `main.py` on crash every 5 min
until 15:15. `PYTHONUTF8=1` is set in the launchers so non-cp1252 log glyphs don't crash
logging on the Windows console.

---

## 3. The four conceptual engines

The system is best understood as four engines, each answering a different question. Three
are fully built; the fourth is partially realized and is the current research frontier.

1. **Morning Bias** — *"Which side is favored today?"* Built. The 08:20 snapshot computes
   OI levels (3 support + 3 resistance), a sentiment/bias, the ATM 0DTE window, a fresh-OI
   positioning heat-map, and relative strength vs QQQ.
2. **Leadership** — *"Who controls the tape right now?"* Built. Per-poll call/put leadership
   scores, cross-strike chain-leadership detection, and an intraday trend tracker.
3. **Activation** — *"Has the move actually begun?"* Built as the **intent gate**: a Gold
   candidate isn't alerted on its event bar — it registers PENDING and is promoted only when
   the next 1–3 bars confirm directional demand and the opposite side doesn't veto.
4. **Absorption** — *"Are institutions accumulating at attractive option prices?"* **Partial.**
   The ingredients exist as gates (near-low §12, volume concentration, premium notional) and
   as the flow-reversal engine (for open positions), but there is no standalone entry engine
   that scores "heavy buying at the intraday value low." Backtesting (see §11) found the
   near-low absorption thesis tested **flat** on available data, so it was not built out.

---

## 4. Daily lifecycle

```
08:10  Task Scheduler → run_scheduled.bat (watchdog) → python main.py
08:20  Morning snapshot (once/day, or catch-up if started late):
         OI levels · sentiment/bias · ATM 0DTE · positioning heat-map · RS vs QQQ
         → Morning Briefing to Discord
08:30  Market open — intraday loop begins
  ↻    Every 60s per symbol: detector.check() → candidates → gates → (maybe) alert + paper trade
14:55  EOD liquidation (0DTE always closed; next-day positions: bank winners, hold strong
         losers overnight, cut weak losers)
post   Daily signal review + gate report + nightly research pipeline
```

The loop (`while True`) runs continuously; time-window checks gate the *actions*, not the
loop itself.

---

## 5. The detection pipeline & gate order

Each poll, per symbol, `detector.check()` fetches the **watched universe** —
`get_watched_contracts` returns the `n` (default 3) nearest strikes per side within a
rolling **±10% band around current spot** (so the universe follows spot, it is not frozen at
the morning ATM). It then runs the entry paths through a short-circuiting gate pipeline:

| # | Gate | Rule (current) | Block reason |
|---|---|---|---|
| 1 | **Location** | spot within proximity of a same-side level | `NOT_NEAR_LEVEL` |
| 2 | **Contract-low / chased (§12)** | `low_dist = mark / min(watched-low, day_low)`; hard block > 2.50 | `CONTRACT_CHASED` |
| 3 | **Volume (two-path)** | Path A dominant absolute **or** Path B contextual conviction (below) | `NO_VALID_VOLUME_SIGNAL:*` |
| 4 | **Historical Value (§13)** | `hv_pctile = (mark−histLow)/(histHigh−histLow)` over full stored history; block if > **0.33** | `HISTORICAL_VALUE_TOO_HIGH` |
| 4b | **Premium Discovery (§13b, PDS)** | virgin/fresh vs recycled/accepted/exhausted — **staged, default OFF** | (Gold gate) |
| 5 | **Short-cover (§14)** | similar-size event much cheaper than a prior → shorts covering | `SHORT_COVER_RISK` |
| 6 | **Already alerted (§19)** | one CALL + one PUT per symbol per day | `ALREADY_ALERTED_TODAY` |
| 7 | **Countertrend / chain** | a bullish reclaim against a still-working down-move needs multi-strike chain confirmation | `COUNTERTREND_*` |
| 8 | **Gold gate** | GRADE · SUBTYPE · INTENT · OPP_VETO · PDS (only when `GOLD_ONLY_PRODUCTION_MODE`) | research-only |

**Two-path volume gate** (`_eval_volume`): absolute volume is always required, a high ratio
is never sufficient alone.
- **Path A — Dominant Absolute:** `peak1m ≥ 750/1000` (NVDA·TSLA higher) OR `vol3m ≥
  1250/1750` OR `vol5m ≥ 1750/2500`, plus EventShare ≥ 0.45, not persistent background,
  near low, premium notional ≥ min.
- **Path B — Contextual Conviction:** moderate volume + extreme ratio (≥8× single / ≥3×
  window) + exact primary level + ATM/1-ITM + near low + concentration + notional.
- Premium notional floor: **$50k 0DTE / $75k next-expiry**. Spam block below
  500/1000/1250 → `INSUFFICIENT_CONVICTION_VOLUME`.

---

## 6. Entry paths

All paths obey the **one CALL + one PUT per symbol per day** dedup (durable across restarts).

- **Primary-level** — the classic path: near one of the 6 morning OI levels, evaluate its
  ATM confirm-side contract (CALL at support, PUT at resistance) through the gates.
- **Chain-led emergent** (`_chain_led_entry`) — coordinated ATM + adjacent-strike volume
  builds a *new* emergent support/resistance before spot reaches a morning level; fires
  without level proximity. Selects ATM by default, or 1-OTM when independently strong + near
  low.
- **Countertrend reversal** — a candidate opposing a still-working, leadership-confirmed
  trend must clear a stricter gate (higher volume floors, opposite-side leadership ≥0.80,
  chain confirmation, a fading-thesis condition). Passes → `PRIMARY_LEVEL_COUNTERTREND_REVERSAL`;
  fails → held as `COUNTERTREND_WATCH` (no alert, no allowance consumed), auto-promoted when
  conviction appears.
- **Opening-event promotion** — an opening-window, event-time-eligible contract can fire when
  its side's opening story is demand-dominant (reuses the chain-led machinery).
- **Chain-leadership** — detects coordinated cross-strike control over the wide watched
  window and trades (or shadow-records) the recommended convexity contract.

---

## 7. Alert taxonomy & the unified Discord card

Every alert is labeled on **three orthogonal axes**, derived deterministically in the engine
(`analysis/alert_taxonomy.py`) — replacing the old grab-bag of overlapping names:

- **Market State** — Compression · Transition · Trend Expansion · Reversal · Breakout.
- **Leadership Type** — Chain Leader · Primary Level · **Gamma Leader** (a gamma ramp:
  accelerating directional bar-range expansion into a near-peak-gamma strike) · Volume Leader.
- **Direction** — CALL / PUT.

Every signal type renders through **one identical Discord card** (`send_signal`): header →
Market State / Leadership / Direction → Why It Triggered (plain reason bullets) → Market
Context → Option Metrics → Trade Plan → System. No stars, no grades, no confidence lines.

---

## 8. Exit & trade management

- **No stop at entry** — the old 50% premium stop whipsawed on 0DTE noise and was removed. A
  **breakeven stop arms only after Exit-1 fills**.
- **Targets** — a price-ordered level ladder; sell half at Exit-1, half at Exit-2, skipping
  any level too close to entry.
- **EOD (14:55)** — 0DTE always closed. Next-day-expiry positions: bank if in profit, hold
  overnight if a strong loser (HIGH + strong cluster, no reversal), cut if a weak loser.

---

## 9. Flow-reversal engine

While a position is open, the **opposite** side of the flow is watched. When it produces a
concentrated volume event (burst out of quiet background, contract near its low) while the
position's side fades, the story has flipped: exit and emit a reversal alert. Auto-flip into
the opposite trade is gated (`FLOW_REVERSAL_AUTO_FLIP`, default off).

---

## 10. Gold-mode chokepoint & staged filters

`analysis/gold_mode.py` is the single production chokepoint. When `GOLD_ONLY_PRODUCTION_MODE`
is on (production default), only a Gold-graded, recognized subtype that passes intent, the
opposite-side veto, and PDS may create a Discord alert + paper trade; everything else is
stored research-only. Each decision produces a **gate-by-gate audit** (`signal_gate_audit`):
`GOLD_MODE · GRADE · SUBTYPE · INTENT · OPP_VETO · PDS → PRODUCTION | RESEARCH`.

**§13b Premium Discovery Score (PDS)** — a staged Gold filter (`analysis/premium_discovery.py`)
that classifies a contract's premium-discovery state from its volume-by-premium history:
`VIRGIN_DISCOVERY` / `FRESH_ACCUMULATION` (eligible) vs `ACCEPTED_VALUE` / `REPRICED_RECYCLED`
/ `EXHAUSTED` (blocked). It rejects a spike into an *already-discovered* premium even at high
volume — the case a min/max range gate scores as "max cheap." **Default OFF**
(`PREMIUM_DISCOVERY_GATE_ENABLED=false`) pending threshold validation.

---

## 11. Instrumentation & diagnostics (built through 2026-07-19)

The recent focus shifted from "add more filters" to "make every decision auditable." Four
pieces of infrastructure now make missed-alert questions answerable as *lookups*:

- **Candidate-coverage log (§73b)** (`candidate_coverage`) — records every *watched* contract
  carrying a ≥250 1-min print, with its distance to the nearest morning level and the poll
  outcome. Flags high-volume **off-level** strikes the level path never evaluated
  (`OFF_LEVEL_NO_ALERT`). Fills the blind spot where only the 6 level strikes were logged.
- **Candidate-bar backfill** (`option_candidate_bars`, `backfill_option_level_bars.py
  --candidates`) — `option_level_bars` only stores the 6 S/R level contracts; this backfills
  1-min Alpaca OHLCV for near-low non-level strikes so outcome tests can price them
  (near-low-moderate coverage went 6% → 100%).
- **Forensic tool** (`candidate_forensics.py`) — `python candidate_forensics.py NVDA 200 CALL
  2026-07-09` reconstructs the per-minute gate trace (base gates + Gold-layer audit +
  leadership + off-level fallback) and prints the exact gate that rejected a contract with
  the values at the time. Turns "why no alert?" into one command.
- **Gate-audit persistence** (`signal_gate_audit`) — the Gold-layer PASS/FAIL verdict is
  stored per signal (already wired via `_persist_signal`).

### What's validated vs staged vs unknown (honest state)

- **Validated / live:** the two-path volume gate, the §12 chased filter (correctly rejected
  the MSFT 395C / GOOGL "chased" examples), the unified taxonomy + card, the exit rule.
- **Staged (built, default-off, pending data):** PDS (§13b) — thresholds are reasoned
  first-guesses, not backtested.
- **Tested and *not* supported:** the "large volume at the option value low" absorption
  thesis — realized returns on near-low + moderate volume were a **coin flip** (48% win, ~0%),
  contradicting the appeal of hand-picked winners. Do not build an absorption engine on it yet.
- **Underpowered / unknown:** tuning the §13 historical-value threshold (0.33 vs 0.40) — only
  ~17 contracts reach §13 in 10 days; > 0.40 is clearly toxic but 0.33-vs-0.40 is n=3 per band
  and cannot be decided on current data. Chased-large prints looked positive (+30%) but on one
  bullish 10-day regime — not robust. **The recurring lesson: the bottleneck is data quantity
  and a genuinely modest edge, not one mis-set threshold.** The instrumentation above exists so
  these questions become answerable *as data accumulates*.

---

## 12. Data model (key tables)

| Table | Holds |
|---|---|
| `option_chain_snapshots`, `near_oi_snapshots` | Morning full-chain + multi-expiry OI |
| `oi_levels` | The 6 S/R levels per symbol per day |
| `price_bars` | 1-min underlying OHLCV + spot + cum volume |
| `option_level_bars` | 1-min OHLCV for the 6 level contracts (Alpaca) |
| `option_hourly_bars` | Hourly option history (PDS input) |
| `option_candidate_bars` | Backfilled 1-min bars for near-low non-level strikes |
| `signal_candidates` (§73) | Every level-path candidate evaluation + blocked_reason + metrics |
| `candidate_coverage` (§73b) | Every watched contract with a volume event + coverage outcome |
| `signal_gate_audit` | Per-signal Gold-layer gate-by-gate verdict |
| `volume_leadership` | Per-minute call/put leadership |
| `session_classification` | A/B/C session type |
| `signals`, `trades`, `signal_outcomes` | Fired signals, executions, labeled outcomes |

---

## 13. Operational runbook

- **Launch:** Task Scheduler `Jakevolume_DailyRun` at 08:10 CST → watchdog bat → `main.py`.
  Single instance (`IgnoreNew`). The loop never self-exits, so a stale process must be stopped
  (`Stop-ScheduledTask`) before it can run stale code into the next session or duplicate.
- **Deploy new code:** merge to `master`, ensure the working tree is on `master` and clean —
  the next 08:10 launch runs whatever is checked out. Stop any running instance so it reloads.
- **Schwab weekly token:** the refresh token expires ~weekly. Verify with
  `python -c "import check_connections as cc; cc.check_schwab()"` (a live quote). If expired,
  re-auth via `schwab_reauth.py` (one browser step). Schwab is needed for the morning OI
  snapshot only.
- **UTF-8:** launchers set `PYTHONUTF8=1` so log glyphs (→) don't crash logging on Windows.
- **Diagnose a missed alert:** `python candidate_forensics.py SYMBOL STRIKE SIDE YYYY-MM-DD`.

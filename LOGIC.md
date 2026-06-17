# Jakevolume — System Logic

End-to-end logic of the <span style="color:#1a7f37">**Simplified V1**</span> Mag-7 call/put alert engine,
broken into small sequential steps. Times are CST.

> **Legend:** <span style="color:#1a7f37">green = added / changed in V1</span> ·
> <span style="color:#d1242f">~~red strikethrough = removed in V1~~</span> · black = unchanged ·
> 🔵 **[Jun-2026]** = post-V1 change (see summary).

---

## 0. Post-V1 updates (June 2026) — current live state

🔵 The system since V1 has these material changes (details in the sections noted):

1. **Intraday data source → Alpaca SIP/OPRA** (§C15, §C19). The live bot uses the
   **Alpaca** market-data client (full SIP stock feed + OPRA options, real per-minute
   option volume, and option price-history). **Schwab is kept only for the morning OI
   snapshot** because Alpaca exposes no live open interest. Databento remains a fallback.
2. **Volume gate rewritten → the 3-rule ENTRY VOLUME GATE FIX** (§D4). `ValidVolumeSignal
   = SingleBarValid OR ClusterValid OR StairStepValid` with explicit thresholds
   (median-robust baseline, visual/cluster dominance, lowered absolute floors), and
   **granular blocked reasons**. (A `VolumeStickoutScore` 0–1 variant was built and
   backtested but **not** wired as the gate — it tested anti-predictive; it remains for
   research only. A VWAP trend-gate and a premium take-profit were also tried and
   **reverted**.)
3. **Spot-anchored morning analysis** (§B6, §B9). The 08:20 anchor is the Alpaca SIP
   **bid/ask mid** (freshest pre-market spot) → Schwab → prev_close; and the sentiment
   P/C band now centers on **spot**, not prev close.
4. **Flow Leadership Reversal Engine + auto-flip** (§L). While a position is open, the
   opposite side is monitored; a confirmed leadership change exits the position and
   **opens the opposite paper trade with its own R2/R3 or S2/S3 targets** (recursive).
5. **Post-close daily review + objective outcome labels + research journal** (§M). At
   15:00 every signal is labeled (return grid, MFE/MAE, EntrySuccess/FalsePositive) and
   a suggested management is recorded; delivered to Discord + Google Sheets.
6. **Discord card shows the trigger volume** (single-bar vs 5-bar window) (§J).

### 🔵 June 16, 2026 — execution & learning fixes (in simple steps)

A backtest of one day's signals showed the right direction was picked (puts ran +31% to +196% favorable) yet every trade still lost −50%. The loss was in the mechanics, not the signal. These changes fix the mechanics and keep the learning data honest:

1. **Removed the 50% stop-loss** (§H). Before: every trade carried a stop at half the entry premium, set the moment it opened. Problem: 0DTE option premium is noisy (bid/ask, theta, vega), so the stop kept firing on noise *before* the trade worked — it whipsawed us out of winners. Now: **no stop at entry**; a breakeven stop arms **only after the first profit target (Exit 1) fills**.

2. **Smart end-of-day for Wednesday (next-day) expiry** (§I). 0DTE positions still always close at 14:55. For positions that expire *later* (still have life): if it's **in profit → bank it**; if it's **losing but the signal was strong** (`confidence HIGH` + `strong_cluster`, no reversal) → **hold it overnight** to give it another day; if it's **losing and weak → cut it**. Winners no longer get dumped early; strong losers get a second chance; weak losers are cut.

3. **Mobile-friendly morning briefing** (§B/§J). The old briefing was one wide text table that wrapped and scrambled the S1/S2/S3 · R1/R2/R3 labels on a phone. Now it's **one Discord embed per symbol**: bias-colored border, previous close / expiry / put-call, then **stacked Support and Resistance lists** with each rank label next to its price. Levels stay in **OI-rank order** (not re-sorted by price).

4. **Aligned the nightly simulation to the new rule** (§M). The post-close review re-labels every signal with "what the live exit rule would have made" (`rule_pnl_pct`), which feeds the Monte-Carlo risk numbers. It was still simulating the deleted 50% stop, so it stamped −50% on trades that actually worked. Now the simulators (`daily_review`, the research backtests) model **no stop + breakeven-after-Exit-1 + ride to EOD** — so the learning labels match reality.

5. **Previous-day historic-low fallback for the §13 gate** (§D5). The §13 "is this option historically expensive?" gate needs the contract's historical low/high. Schwab serves no option price-history, so the gate was silently doing nothing. Now, when no live history exists, the detector **fetches the contract's previous session's low/high from the database** (`option_level_bars`) and uses that — a 0DTE strike falls back to **yesterday's same-strike** contract.

6. **Daily-review safety + a bug fix** (§M). (a) Fixed a latent bug that silently broke the `signal_volume_analytics` write on every review (`execute_values` had too many `%s` placeholders). (b) Added a **guard**: a re-run that can't rebuild a trade's price path (e.g. an expired 0DTE contract the broker no longer serves) **no longer overwrites already-computed metrics with NULLs** — prior results are kept.

---

## A. Scheduling & startup

1. **Windows Task Scheduler** launches `run_scheduled.bat` at **08:10 CST** daily.
2. The bat is a **watchdog** — it restarts `main.py` on crash every 5 min until 15:15.
3. `main.py` boots: connects the Postgres pool, runs `init_schema()`, logs into Schwab (token auto-refresh), connects Google Sheets, optionally inits Databento/Alpaca.
4. It enters a **60-second loop**. Each tick checks time windows: snapshot (08:20), market hours (08:30–15:00), EOD (14:55).
5. <span style="color:#1a7f37">**[NEW] Catch-up snapshot** — if the process starts/restarts *after* the 08:20 window (e.g. a watchdog crash-restart) and today has no snapshot yet, the morning snapshot runs immediately as a catch-up, so a late start still gets levels + a briefing. Skipped (no duplicate) if today's levels already exist in the DB.</span>

---

## B. Morning snapshot — once per day at 08:20 CST (or catch-up)

For **each symbol** <span style="color:#d1242f">~~(9 symbols incl. SPY, QQQ)~~</span> <span style="color:#1a7f37">(7 Mag-7: AAPL, MSFT, AMZN, GOOGL, META, NVDA, TSLA)</span>:

6. **Fetch prices** — `prev_close` and `pm_price` (current/pre-market last) from Schwab.
7. **Fetch the option chain** — nearest expiry with both calls and puts. Mon/Wed/Fri = **today (0DTE)**; Tue/Thu = **next expiry (1DTE)**.
8. **Compute the 6 OI levels** anchored to the **8:20 spot** (`pm_price`):
   - <span style="color:#d1242f">~~ATM = nearest strike. R1 = nearest call above ATM; R2/R3 = higher-OI of the next 2-strike windows. S1 = nearest put ≤ ATM; S2/S3 = same window rule down.~~</span>
   - <span style="color:#1a7f37">**[CHANGED] Within ±5% of spot, rank by OI, take the top 3 per side:** CALL strikes in `[spot, spot×1.05]` ranked by call OI → **R1/R2/R3** (R1 = highest OI). PUT strikes in `[spot×0.95, spot]` ranked by put OI → **S1/S2/S3**. Rank 1 = highest OI, *not* nearest. Out-of-band strikes are ignored.</span>
   - Each level stores: type, rank (1–3), strike, OI, option_type, expiry.
9. **Compute sentiment** — pre-market drift + put/call OI ratio → bias.
10. **Top-OI snapshot** — 2 highest-OI call & put strikes near ATM (reference).
11. **Persist to Postgres** — chain snapshot, the 6 OI levels, morning sentiment.
12. **Log to Google Sheets** — daily levels, OI snapshot, sentiment, comparison row.
13. **Print + send the briefing** — console table + Discord morning message. 🔵 **[Jun-16 CHANGED] The Discord briefing is now mobile-friendly: one embed per symbol** (bias-colored border) with **stacked Support/Resistance fields** — each level labelled beside its value (`S1: $295.00`), kept in **OI-rank order** (never silently price-sorted). Replaces the old wide monospaced code-block table that wrapped and detached labels on mobile. Overnight OI buildup rides along as a compact trailing embed.
14. **Retention prune** — keep the most recent **10 trading days** of 1-min data.

---

## C. Intraday loop — every 60 s during 08:30–15:00

For **each symbol**:

15. **Equity bars** — Schwab pulls the **full session** (`SESSION_BARS=400`); detector gets the trailing 40 (`BARS_TO_FETCH`); full set persisted to `price_bars`.
16. **Staleness guard** — skip the symbol if the newest bar isn't today's or is > `MAX_BAR_AGE_SECONDS` (300 s) old.
17. `underlying_price` = freshest live quote (fallback: last bar close).
18. **Load today's 6 levels** from Postgres. If none, skip the symbol.
19. **Watched option quotes** — Schwab returns the **3 nearest strikes per side** to spot, each with bid/ask/mark/volume/OI/day-high/day-low.
20. **(Tue/Thu only)** fetch the **full chain** so the detector can price a target OTM strike.
21. **Collect level-option bars** — 1-min OHLCV for the 6 level contracts → `option_level_bars`.
22. **Run the detector** (Section D) → 0 or 1 signal per direction.
23. For each signal: save to `signals`, log to Sheets, send Discord/desktop notification, and **auto-trade** if Alpaca is enabled. <span style="color:#d1242f">~~(unless WATCH or an upgrade)~~</span> <span style="color:#1a7f37">(every emitted V1 signal is actionable — there are no WATCH/upgrade signals)</span>.
24. **Check exits** on open trades (Section H).

---

## D. The detection pipeline (`detector.check`) — per symbol, per bar

### D1. Setup
25. Take the latest bar; `close_price` = its close; `today` = its date.
26. `next_day_mode = NEXT_DAY_MODE_ENABLED and expiry > today` (1DTE / Tue·Thu).
27. **Daily reset** of all intraday state when the date changes <span style="color:#1a7f37">(incl. the historical-range cache and short-cover event store)</span>.
28. **Durable dedup fold-in** — read directions already fired today from the `signals` table and mark them fired (survives restarts / a 2nd instance).
29. <span style="color:#1a7f37">**[CHANGED] Opening range (§15)** — during the first `OPENING_RANGE_MINUTES` (15) after open, the bar is **not** suppressed; instead the volume thresholds below are *raised*. <span style="color:#d1242f">~~(Old: a 5-min warm-up emitted nothing.)~~</span></span>

### D2. Per-contract volume bookkeeping (all watched quotes)
30. For each watched `(strike, type)`: read cumulative day volume.
31. **Discontinuity guard** — if not seen on the previous bar (gap > 1.5× poll), treat re-entry as fresh: **delta = 0**, clear history.
32. Otherwise **delta = current − previous cumulative** (≥ 0) — the "1-minute volume".
33. Append delta to the contract's rolling history; track its lowest mark.

### D3. Per-level loop — for each of the 6 levels
34. <span style="color:#d1242f">~~Effective role: 0DTE keeps the frozen type; next-day flips role by spot position with a deadband.~~</span> <span style="color:#1a7f37">**[CHANGED] Role is always the frozen morning type** (SUPPORT/RESISTANCE). Dynamic S/R flipping removed.</span>
35. From role: `confirm_type` (CALL at support / PUT at resistance) and `signal_type` (BULLISH/BEARISH).
36. <span style="color:#d1242f">~~Proximity score by distance band (0.25%→1.0, 0.35%→0.7, 0.50%→0.5).~~</span> <span style="color:#1a7f37">**[CHANGED] Binary proximity** — `NearLevel` if `|spot−level|/spot ≤ 0.0035` (default) or `≤ 0.0050` for **TSLA/NVDA**. Not near → log `NOT_NEAR_LEVEL`, skip.</span>
37. **Pick ATM + 1 ITM** confirm-side contracts (ATM nearest spot; ITM one strike in-the-money).

### D4. Volume validation — a valid signal needs **any one** of §9/§10/§11 (per contract, ATM or ITM)
38. **§9 Extreme single print** — `delta ≥ floor` (300 / 750 NVDA·TSLA) **AND** `delta / max(avgPrior10,10) ≥ 8×` **AND** `low_dist ≤ 1.75`.
39. **§10 Cluster** (last 5 deltas) — <span style="color:#1a7f37">**[ADDED] `WindowVol5 ≥ floor` (300 / 600)** **AND**</span> `WindowRatio5 ≥ 3×` **AND** `ActiveBars5 (per-bar ≥ 2×) ≥ 3` **AND** `low_dist ≤ 1.75`.
40. <span style="color:#1a7f37">**[NEW] §11 Stair-step accumulation** — `ExcitationScore ≥ 0.70` **AND** `WindowVol5 ≥ floor (300/600)` **AND** `WindowRatio5 ≥ 2.5` **AND** `ActiveBars5 ≥ 3` **AND** `low_dist ≤ 2.0`, where `ExcitationRaw = 1.0·r[t] + 0.6·r[t-1] + 0.35·r[t-2] + 0.20·r[t-3] + 0.10·r[t-4]` and `ExcitationScore = min(ExcitationRaw,10)/10`. The absolute `WindowVol5` floor (same as the cluster path) stops it firing on ratios alone in a quiet contract.</span>
41. **§12 Contract-low filter** — `low_dist = mark / min(watched-low, day_low)`; preferred ≤ 1.75 (required by §9/§10); **hard block** if > 2.50 (chased).
42. <span style="color:#1a7f37">**[CHANGED, opening range]** during the first 15 min: single-print floor ×1.5, cluster `WindowRatio5 ≥ 4.0`, stair-step `ExcitationScore ≥ 0.80`.</span>
43. `valid_volume = (ATM passes any of §9/§10/§11) OR (ITM passes any)`.

### D5. Entry gates (block → no alert; §21 logs the reason)
44. <span style="color:#d1242f">~~Spread gate — block if (ask−bid)/mid > 50%.~~</span> <span style="color:#d1242f">**[REMOVED]** (kept only as a logged field)</span>
45. <span style="color:#d1242f">~~Target-room gate — require room to the nearest opposing level.~~</span> <span style="color:#d1242f">**[REMOVED]**</span>
46. **Chased** — ATM `low_dist > 2.50` → block `CONTRACT_CHASED`.
47. No valid volume → block `NO_VALID_VOLUME_SIGNAL`.
48. <span style="color:#1a7f37">**[CHANGED] §13 Historical value percentile** — on the contract you'd buy: `pctile = (mark − HistLow)/(HistHigh − HistLow)` over the multi-day window (Schwab daily candles, cached/day). `pctile > 0.60` → block `HISTORICAL_VALUE_TOO_HIGH`. 0DTE has no history → gate skipped. <span style="color:#d1242f">~~(Old: `mark/hist_low ≤ 1.25`, and a failure only downgraded to WATCH.)~~</span></span>
    - 🔵 **[Jun-16 NEW] Previous-session fallback** — when no live multi-day history exists (Schwab serves no option price-history, so the gate was a silent no-op), the detector falls back to the contract's **previous session's `(low, high)`** from `option_level_bars` (`db.get_option_prev_range`), so the gate can still evaluate. Matched by strike + type (not expiry), so a 0DTE contract uses **yesterday's same-strike** contract. Cached per contract/day.
49. <span style="color:#1a7f37">**[NEW] §14 Short-cover risk** — store prior *major* volume events per contract. If the current major event has `VolumeSimilarity ∈ [0.70, 1.50]` vs a prior event **and** `CurrentPrice/PriorPrice ≤ 0.50` (similar size, much cheaper now → shorts covering), block `SHORT_COVER_RISK`.</span>
50. **§19 Already alerted** this direction today → block `ALREADY_ALERTED_TODAY`.

### D6. Trade contract
51. <span style="color:#1a7f37">**[CHANGED] Trade the ATM confirm-side contract at/near the level** (the same strike volume was detected on) in **both** 0DTE and next-day mode. `traded_strike` = that contract's strike, so the label, quote, and price always agree. <span style="color:#d1242f">~~(Removed: the 1DTE OTM target-shift that bought a further-OTM strike toward the next level — it made the traded strike differ from the level on Tue/Thu.)~~</span></span>

---

## E. <span style="color:#d1242f">~~Classification & confidence~~</span> <span style="color:#1a7f37">Alert decision — single boolean</span>

52. <span style="color:#d1242f">~~Priority tiers: HIGH / MEDIUM_HIGH / MEDIUM / WATCH, each with spread+room gates; WATCH recorded but never traded.~~</span> <span style="color:#1a7f37">**[REMOVED all tiers.]** A level that passes every gate (§4 near + §5 side + §8 valid volume + §12 not chased + §13 not rich + §14 no short-cover + §19 not yet fired) produces **one actionable signal** (`confidence = HIGH`). No WATCH, no upgrades.</span>
53. Build the full signal dict (option prices, **shifted exits — Section G**, day_mode, traded_strike, target_level, the §21 metrics, etc.).

---

## F. Fire decision — one CALL + one PUT per ticker per day

54. Across the in-range levels this bar, keep the **strongest per direction**: lowest rank (highest-OI level) then largest ATM 1-min volume.
55. <span style="color:#d1242f">~~Upgrade path / WATCH→real promotion.~~</span> <span style="color:#1a7f37">**[REMOVED].**</span> Fire only if this direction hasn't fired today; then mark it fired.
56. **Durable dedup** backs the one-per-direction guarantee across restarts / overlapping instances (DB is the backstop; the single-instance lock is the first line).
57. Net: **at most one CALL and one PUT symbol per ticker per day.**

---

## G. 🔵 **[Jun-2026] Exit targets — full ladder, skip-only-if-too-close**

58. 🔵 **[CHANGED] The exit ladder is ALL levels the trade moves into — not only the opposing side — and a level is skipped *only when it is too close to the entry*** (we no longer always skip the nearest). A CALL climbs up through every level above the entry; a PUT falls through every level below it:
    - **Call entered ~S3:** ladder = S2, S1, R1, R2, R3. If S2 has room → Exit1=S2, Exit2=S1. If S2 is too close → Exit1=S1, Exit2=R1.
    - **Call ~S2** (S1 too close) → Exit1=R1, Exit2=R2. **Call ~S1** (R1 too close) → Exit1=R2, Exit2=R3.
    - **Mirror for PUTs** entered at R3/R2/R1 (ladder falls R2,R1,S1,S2,S3 …).
    - <span style="color:#d1242f">~~(V1: always skip the nearest opposing level, use 2nd/3rd opposing only — ignored same-side levels above the entry.)~~</span>
59. **Too close** = within `EXIT_MIN_ROOM_PCT` (0.25%) of the entry spot. Exit1/Exit2 = the first two ladder levels that clear it. Goal: don't sell the first half too soon, and capture the meat of the move.
60. **Fallbacks:** if every level is too close, keep the raw nearest two; only one level on the move side → Exit2 = null.
61. These exits are shown on the Discord card and drive the exit state machine. **Trade-quality is logged** in `signal_outcomes`: `entry_vs_lod` (entry ÷ the contract's low *up to entry* — 1.0 = bought the low, >1 = chased) and `pct_peak_captured` (current-rule P&L as a % of the peak/MFE — "are we capturing ~80% of the move?").

---

## H. Trade execution & exit management (Alpaca on)

62. Strike = `traded_strike`; entry = the contract's ask. **Skip** if no price/expiry, at `MAX_OPEN_POSITIONS` (atomic via `max(alpaca count, DB open count)`), or portfolio too small for 1 contract.
63. <span style="color:#1a7f37">**[NEW] Entry-fill guard** — before managing exits, confirm Alpaca actually holds the contract (`position_qty(occ) > 0`); an unfilled limit entry never triggers phantom "uncovered" exit orders.</span>
64. Quantity split half/half. 🔵 **[Jun-16 CHANGED] No stop-loss at entry.** <span style="color:#d1242f">~~stop-loss = 50% of entry~~</span> — the 50% premium stop was removed because 0DTE premium noise (bid/ask + theta + vega) whipsawed it out of correct trades before the thesis played out. A breakeven stop is armed **only after Exit 1** fills.
65. <span style="color:#d1242f">~~Stop-loss first — mark ≤ stop → close remaining qty.~~</span> 🔵 **[Jun-16]** Until Exit 1 fills there is no stop; the position rides to a target or to EOD.
66. **Exit 1** — underlying reaches the **shifted** Exit1 (R2/S2): sell half, raise stop to breakeven; opposite-side cluster at target → close remainder early.
67. **Exit 2** — underlying reaches the **shifted** Exit2 (R3/S3), or the early opposite-side trigger: sell the remainder.

---

## I. EOD liquidation — 14:55 CST

68. **0DTE positions** (expire today) are always closed.
69. 🔵 **[Jun-16 NEW] Smart EOD for Wednesday (next-day) expiry positions** — these still have life left, so instead of always flattening:
    - **In profit** → close and bank it (even if it never hit the R/S target).
    - **At a loss + strong** (`confidence = HIGH` **AND** `strong_cluster`, and no reversal) → **hold overnight**, any loss size, to give it the remaining day.
    - **At a loss + weak** → close (cut it).
    - Profit/loss is read live from Alpaca (`position_unrealized_pl`); unknown P&L → close (never hold on bad data). With `EOD_CLOSE_NEXT_DAY=false` the legacy "hold all next-day" behavior still applies.

---

## J. Discord card (§20)

69. <span style="color:#1a7f37">**[CHANGED] Simplified card:**</span>
    ```
    AAPL 315P @ 1.40
    Spot: 315.20
    Level: R1 315
    Volume: 497
    Ratio: 12.4x
    ContractLowDistance: 1.18
    Exit 1/2 @ 265
    Exit rest @ 262.50
    ```
    <span style="color:#d1242f">~~No volume-shape label, spread, or target-room shown.~~</span> <span style="color:#1a7f37">(The volume *kind* — single / cluster / stair-step — is computed internally but never surfaced.)</span>

---

## K. What's stored (Postgres)

70. `price_bars`, `option_level_bars`, `option_chain_snapshots`, `oi_levels`, `morning_sentiment`, `signals`, `trades`, `volume_clusters`.

---

## Notes & caveats

- **"1-minute volume" is an approximation** — a quote-delta sampled each poll, robust to strike rotation but cadence-sensitive.
- **Stop-loss is a soft, poll-based mark check** (50% hardcoded), not a resting broker order.
- <span style="color:#1a7f37">**§13 and §14 are data-dependent** (Schwab daily candles / intraday event history) — worth watching the `MONITOR … → <REASON>` logs early to confirm they gate rather than over-block.</span> 🔵 **[Jun-16]** §13 no longer goes fully dark without live history — it falls back to the previous session's option low/high from the DB (see §D5.48).

---

## Key config knobs (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `SNAPSHOT_HOUR` / `SNAPSHOT_MINUTE` | 08:20 | Morning snapshot time |
| <span style="color:#1a7f37">`OI_LEVEL_BAND_PCT`</span> | <span style="color:#1a7f37">0.05</span> | <span style="color:#1a7f37">±5% band for top-3-by-OI levels</span> |
| <span style="color:#1a7f37">`NEAR_LEVEL_DIST_DEFAULT` / `_VOLATILE`</span> | <span style="color:#1a7f37">0.0035 / 0.0050</span> | <span style="color:#1a7f37">Binary proximity (TSLA/NVDA wider)</span> |
| `OPT_SINGLE_PRINT_RATIO` | 8.0 | Single-print ratio threshold |
| `OPT_MIN_SINGLE_PRINT_VOL` | 300 / 750 | Per-symbol single-print floors |
| `OPT_CLUSTER_WINDOW` / `_WINDOW_RATIO` / `_ACTIVE_MIN` | 5 / 3.0 / 3 | Cluster window / ratio / active bars |
| <span style="color:#1a7f37">`OPT_MIN_CLUSTER_WINDOW_VOL`</span> | <span style="color:#1a7f37">300 / 600</span> | <span style="color:#1a7f37">Absolute WindowVol5 floor (§10)</span> |
| <span style="color:#1a7f37">`STAIRSTEP_WEIGHTS` / `_EXCITATION_MIN` / `_WINDOW_RATIO_MIN`</span> | <span style="color:#1a7f37">1/.6/.35/.2/.1 · 0.70 · 2.5</span> | <span style="color:#1a7f37">Stair-step accumulation (§11)</span> |
| `NEAR_LOW_MAX_DIST` / `CONTRACT_LOW_MAX_DIST` | 1.75 / 2.50 | NearLow / chased block |
| `HIST_LOW_ENTRY_GATE` | true | Enable the historical-value gate |
| `OPT_HIST_LOOKBACK_DAYS` | 10 | Days of daily candles |
| <span style="color:#1a7f37">`HIST_VALUE_PCTILE_MAX`</span> | <span style="color:#1a7f37">0.60</span> | <span style="color:#1a7f37">Block if value percentile above this (§13)</span> |
| <span style="color:#1a7f37">`SHORT_COVER_FILTER` / `_SIM_LOW/HIGH` / `_REPRICE_MAX`</span> | <span style="color:#1a7f37">true · 0.70/1.50 · 0.50</span> | <span style="color:#1a7f37">Short-cover risk filter (§14)</span> |
| <span style="color:#1a7f37">`OPENING_RANGE_MINUTES` / `_VOL_MULT` / `_CLUSTER_RATIO` / `_EXCITATION_MIN`</span> | <span style="color:#1a7f37">15 · 1.5 · 4.0 · 0.80</span> | <span style="color:#1a7f37">Opening-range raised thresholds (§15)</span> |
| <span style="color:#1a7f37">`EXIT_MIN_ROOM_PCT`</span> | <span style="color:#1a7f37">0.0025</span> | <span style="color:#1a7f37">Min room after the exit-target shift</span> |
| `NEXT_DAY_MODE_ENABLED` | true | Tue/Thu use the next expiry (1DTE); <span style="color:#1a7f37">trades at the level like 0DTE</span> |
| <span style="color:#d1242f">~~`NEXT_DAY_TARGET_DEPTH`~~</span> | <span style="color:#d1242f">~~1~~</span> | <span style="color:#d1242f">**[REMOVED]** OTM target-shift no longer used</span> |
| `EOD_CLOSE_NEXT_DAY` | true | Close next-day positions at EOD |
| `BAR_RETENTION_DAYS` | 10 | Trading days of 1-min data kept |
| `MAX_OPEN_POSITIONS` / `TRADE_PCT` | 3 / 0.01 | Position cap / size per trade |
| <span style="color:#d1242f">~~`PROX_BAND_TIGHT/MID/WIDE`~~</span> | <span style="color:#d1242f">~~0.0025/.0035/.0050~~</span> | <span style="color:#d1242f">**[REMOVED]** tiered proximity → binary</span> |
| <span style="color:#d1242f">~~`MAX_SPREAD_PCT` / `TARGET_ROOM_*`~~</span> | <span style="color:#d1242f">~~0.50 / …~~</span> | <span style="color:#d1242f">**[REMOVED]** spread + target-room gates</span> |
| <span style="color:#d1242f">~~`SINGLE_PRINT_RANKS` / `CLUSTER_UPGRADE_ENABLED` / `EMIT_UPGRADE_ALERT` / `EMIT_WATCH_ONLY`~~</span> | <span style="color:#d1242f">~~…~~</span> | <span style="color:#d1242f">**[REMOVED]** tiers / WATCH / upgrades</span> |
| <span style="color:#d1242f">~~`LEVEL_FLIP_DEADBAND_PCT` / `HIST_LOW_NEAR_RATIO`~~</span> | <span style="color:#d1242f">~~0.0015 / 1.25~~</span> | <span style="color:#d1242f">**[REMOVED]** S/R flipping / old hist-low ratio</span> |

> Removed config constants are retained (unused) in `config.py` only so older `test_*.py` imports keep working.

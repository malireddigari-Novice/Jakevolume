# Jakevolume — System Logic

End-to-end logic of the 0DTE / next-day OI-alerting system, broken into small
sequential steps. Reflects the current code. Times are CST.

---

## A. Scheduling & startup

1. **Windows Task Scheduler** launches `run_scheduled.bat` at **08:10 CST** daily.
2. The bat is a **watchdog** — it restarts `main.py` on crash every 5 min until 15:15.
3. `main.py` boots: connects the Postgres pool, runs `init_schema()` (idempotent migrations), logs into Schwab (token auto-refresh), connects Google Sheets, optionally inits Databento/Alpaca.
4. It enters a **60-second loop**. Each tick checks three time windows: snapshot (08:20), market hours (08:30–15:00), EOD (14:55). The 10-minute gap between launch (08:10) and snapshot (08:20) is deliberate warm-up.

---

## B. Morning snapshot — once per day at 08:20 CST

For **each of the 9 symbols** (AAPL, MSFT, AMZN, GOOGL, META, NVDA, TSLA, SPY, QQQ):

5. **Fetch prices** — `prev_close` (yesterday's close) and `pm_price` = current/pre-market last price from Schwab (`pm_price = lastPrice or prev_close`).
6. **Fetch the option chain** — Schwab's nearest expiry that has both calls and puts. On Mon/Wed/Fri this is **today (0DTE)**; on Tue/Thu it's the **next available expiry**.
7. **Compute the 6 OI levels** anchored to the **8:20 spot** (`pm_price`):
   - ATM = strike nearest spot.
   - **R1** = nearest call strike above ATM; **R2** = higher-OI of the next 2 call strikes; **R3** = higher-OI of the pair after that.
   - **S1** = nearest put strike at/below ATM; **S2/S3** = same window rule going down.
   - Each level stores: type (SUPPORT/RESISTANCE), rank (1–3), strike, OI, option_type, expiry.
8. **Compute sentiment** — pre-market drift (`pm_price` vs `prev_close`) + put/call OI ratio → bias (BULL/BEAR/NEUTRAL).
9. **Top-OI snapshot** — the 2 highest-OI call and put strikes near ATM (reference).
10. **Persist to Postgres** — full chain snapshot, the 6 OI levels, morning sentiment (all anchored to `pm_price`).
11. **Log to Google Sheets** — daily levels, OI snapshot, sentiment, comparison row.
12. **Print + send the briefing** — console table + Discord morning message.
13. **Retention prune** — keep only the most recent **10 trading days** of 1-min data (`price_bars`, `option_level_bars`); delete older. Alerts/signals are never pruned. No-op until 10 days exist.

---

## C. Intraday loop — every 60 s during 08:30–15:00

For **each symbol**:

14. **Equity bars** — Schwab pulls the **full session** of 1-min bars (`SESSION_BARS=400`). The detector is handed only the trailing 40 (`BARS_TO_FETCH`); the full set is persisted.
15. **Persist 7 fields/bar** to `price_bars`: open, high(max), low(min), close, volume(per-min), spot_price, cum_volume (running session total; NULL on the partial Databento path).
16. `underlying_price = last bar's close`.
17. **Load today's 6 levels** from Postgres. If none, skip the symbol.
18. **Watched option quotes** — Schwab returns the **3 nearest strikes per side** to spot (so a real ATM + ITM pair exists), each with bid/ask/mark/volume/OI/day-high/day-low.
19. **(Tue/Thu only)** fetch the **full option chain** (`chain_quotes`) so the detector can price an OTM target strike.
20. **Collect level-option bars** — for each of the 6 level contracts, pull 1-min OHLCV and upsert to `option_level_bars` (full-session, self-backfilling).
21. **Get the morning P/C ratio** (conviction context).
22. **Run the detector** (Section D) → 0 or 1 signal.
23. For each returned signal: save to `signals`, log to Sheets, send Discord/desktop notification, and **auto-trade** if it's actionable (not WATCH, not an upgrade, Alpaca enabled).
24. **Check exits** on open trades (Section H).
25. **Positioning monitor** (Databento) — tracks unusual cluster accumulation without firing signals.

---

## D. The detection pipeline (`detector.check`) — per symbol, per bar

### D1. Setup
26. Take the latest bar; `close_price` = its close; `today` = its date.
27. **Determine mode**: `next_day_mode = NEXT_DAY_MODE_ENABLED and expiry > today` (i.e., no 0DTE today = Tue/Thu).
28. **Daily reset** of all intraday state when the date changes.

### D2. Per-contract volume bookkeeping (all watched quotes)
29. For each watched `(strike, type)`: read cumulative day volume.
30. **Discontinuity guard** — if this contract wasn't seen on the previous bar (gap > 1.5× poll interval), treat re-entry as fresh: **delta = 0** and clear its history (prevents fake spikes from strike rotation).
31. Otherwise **delta = current_cumulative − previous_cumulative** (clamped ≥ 0) — the proxy for "1-minute volume".
32. Append delta to that contract's rolling history (deque of 15).
33. Track the contract's **lowest mark** seen.

### D3. Per-level loop — for each of the 6 levels
34. **Effective role**:
    - 0DTE: use the frozen morning type (SUPPORT/RESISTANCE).
    - Next-day: role = position vs spot, with a **deadband** — clearly above strike → SUPPORT; clearly below → RESISTANCE; inside the band → keep frozen role.
35. From role: `confirm_type` (CALL at support / PUT at resistance) and `signal_type` (BULLISH/BEARISH).
36. **Proximity gate** — score by distance: ≤0.25%→1.0, ≤0.35%→0.7, ≤0.50%→0.5, beyond→**skip** (out of range).
37. **Pick ATM + 1 ITM** confirm-side contracts: ATM = nearest strike to spot; ITM = nearest strike genuinely in-the-money (below spot for calls, above for puts).

### D4. Volume validation (per contract — ATM and ITM)
38. **Single print** — valid if `delta ≥ floor` (300 big-caps / 750 NVDA·TSLA) **AND** `delta / max(avgPrior10, 10) ≥ 8×`.
39. **5-bar cluster** — over the last 5 deltas: `WindowRatio = sum / (5 × max(avgPrior10,10)) ≥ 3×` **AND** `ActiveBars (per-bar ratio ≥ 2×) ≥ 3`.
40. **Contract-low filter** — `low_dist = mark / min(watched-low, day_low)`; **NearLow** if ≤1.75 (required to qualify a print); **TooChased** if >2.50 (hard block on the ATM).
41. **Near-low-qualified validity**: `atm_single`, `atm_cluster`, `itm_single`, `itm_cluster` (each = raw validity AND that contract is near its low).
42. **Combine**:
    - `atm_itm_confirm` = ATM valid **and** ITM valid
    - `cluster_valid` = ATM **or** ITM cluster
    - `extreme_single` = ATM **or** ITM near-low single print

### D5. Hard gates
43. If nothing notable (no raw single, no raw cluster) → skip the level.
44. If the ATM contract is **TooChased** (>2.50) → skip (don't chase).

### D6. OTM strike (next-day only)
45. Default trade contract = the ATM-near-spot contract; `traded_strike` = the level strike.
46. If next-day + full chain available: find the **target level** (nearest opposing level beyond spot — at S3, that's S2) and pick the chain strike nearest it → that becomes the **OTM contract** you'd actually buy.

### D7. Soft gates (downgrade, don't discard)
47. **Spread** — on the contract you'd trade; OK if ≤ 50% of mid.
48. **Target room** — distance to the nearest opposing level (position-based in next-day mode); scored; `room_ok` if > 0.

---

## E. Classification & confidence (per qualifying level)

49. Evaluate in priority order (all require spread + room OK to be actionable):
    - **HIGH / `ATM_ITM_CLUSTER`** — `atm_itm_confirm` AND `cluster_valid`.
    - **MEDIUM_HIGH / `EXTREME_SINGLE_PRINT`** — extreme single at S2/S3 or R2/R3.
    - **MEDIUM / `VOLUME_PRESSURE_CLUSTER`** — single-side cluster.
    - **MEDIUM / `EXTREME_SINGLE_PRINT`** — extreme single at rank 1.
    - **WATCH / `RANDOM_SINGLE_PRINT`** — notable but missing a condition (not near low / no room / no ITM). Recorded + notified, **never traded**.
49a. **Historical-low entry gate** (`HIST_LOW_ENTRY_GATE`, on by default) — before an actionable entry stands, the contract you'd actually buy (the OTM target in next-day mode) must trade at/near its **multi-day historical low**: `mark / hist_low ≤ HIST_LOW_NEAR_RATIO` (1.25). `hist_low` is the lowest daily candle over `OPT_HIST_LOOKBACK_DAYS` (10) pulled from Schwab, fetched once per contract per day and cached. **0DTE contracts have no prior-day history → gate is a no-op those days** (only bites Tue/Thu next-day mode). A failing entry is **downgraded to WATCH** (still alerted, not auto-traded), never silently dropped.
50. Build the full signal dict (option prices, exits, P/C conviction, option H/L flag, day_mode, traded_strike, target_level, etc.) and add it to the bar's candidate list.

---

## F. Fire decision — one CALL + one PUT per ticker (`_fire_decision` + selection)

51. **One alert per direction per ticker per day**, keyed on `(symbol, direction)`:
    - Never fired this direction → **fire**.
    - Actionable after a prior WATCH → **fire** (the real call/put entry).
    - Stronger same-direction signal, only if `EMIT_UPGRADE_ALERT` is on → **upgrade** (a second alert, flagged so it isn't re-traded).
    - Otherwise (direction already alerted) → **skip**.
52. Among eligible candidates this bar, pick **actionable first, then highest confidence, then most room** — so the best bullish setup becomes the single call symbol and the best bearish setup the single put symbol.
53. Record the best rank fired for that direction; return exactly that one signal.
53a. **Durable dedup** — at the top of each `check`, the directions already fired today are read back from the `signals` table (`get_fired_directions_today`) and folded into `_fired_today` (max confidence per direction). The in-memory state alone is **per-process and lost on restart**, so without this a watchdog restart — or a second concurrent instance — would re-fire the same call/put. With it, the first fire is persisted and every later bar/process sees it and skips. Defends the one-per-direction guarantee across restarts and overlapping instances (the lock is the first line; this is the backstop).
54. Net effect: **at most one CALL and one PUT symbol per ticker per day** (plus an optional upgrade follow-up only when `EMIT_UPGRADE_ALERT=true`).

---

## G. Trade execution (actionable, non-upgrade, Alpaca on)

54. Strike = `traded_strike` (OTM in next-day mode, else the level strike); entry price = the contract's ask.
55. **Skip** if: no entry price, no expiry, next-day OTM unresolved, at `MAX_OPEN_POSITIONS`, or portfolio too small for 1 contract.
56. **Exit targets** = the signal's own `exit1_price`/`exit2_price` (honors the next-day flip); **skip the trade if no exit target exists**.
57. Quantity split: half at exit 1, remainder at exit 2.
58. **Stop-loss = 50% of entry premium** (set at entry).
59. Place the buy-to-open order; persist the trade row; log to Sheets.

---

## H. Exit management (every bar, per open trade)

60. **Stop-loss first** — if current mark ≤ stop, close the **entire remaining** qty, mark stopped. (Soft, poll-based.)
61. **Exit 1** — when underlying reaches R1/S1: sell half, then **raise the stop to breakeven (entry)**.
62. **Opposite-side check** — after exit 1, if opposite-side volume clusters at the target, close the remainder early.
63. **Exit 2** — when underlying reaches R2/S2 (or the early opposite-side trigger): sell the remainder.

---

## I. EOD liquidation — 14:55 CST

64. Close all 0DTE positions. With `EOD_CLOSE_NEXT_DAY=true` (default), **next-day positions are also closed** — no overnight hold.

---

## J. Retention

65. Once per day (end of the morning snapshot), prune `price_bars` + `option_level_bars` to the **last 10 trading days** (`BAR_RETENTION_DAYS`); signals/trades untouched.

---

## K. How Tue/Thu (next-day mode) changes the above

66. **Expiry** rolls to next-day automatically (Step 6).
67. **Levels flip by spot position** each bar, with a deadband (Step 34) — sell into S3 ⇒ S2/S1 act as resistance; push into R3 ⇒ R2/R1 act as support.
68. **Strike is OTM at the target level** (Step 46) — at S3 you buy the contract at S2; detection volume still comes from the spot-side contracts.
69. **Exits + room are position-based** (Steps 48, 56); the trade exits at the flipped levels.
70. Positions still **close at EOD** (`EOD_CLOSE_NEXT_DAY`).

---

## L. What's stored (Postgres)

71. `price_bars` (per-min equity OHLCV + spot + cum_volume), `option_level_bars` (per-min OHLCV of the 6 level contracts), `option_chain_snapshots`, `oi_levels`, `morning_sentiment`, `signals` (with confidence/shape/day_mode/traded_strike/target_level/upgrade), `trades`, `volume_clusters`.

---

## Notes & caveats

- **"1-minute volume" is an approximation** (Steps 31, 38–39): a quote-delta sampled each poll, not a true candle. It's robust to strike rotation now (Step 30) but still cadence-sensitive between polls.
- **Stop-loss is a soft, poll-based mark check** (Step 60), not a resting broker order — a fast gap between polls can overshoot the 50% level. The 50% is currently hardcoded in `_execute_trade`.
- **WATCH alerts and cluster upgrades are never auto-traded** (Steps 49, 51).

---

## Key config knobs (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `SNAPSHOT_HOUR` / `SNAPSHOT_MINUTE` | 08:20 | Morning snapshot time |
| `OPT_SINGLE_PRINT_RATIO` | 8.0 | Single-print ratio threshold |
| `OPT_MIN_SINGLE_PRINT_VOL` | 300 / 750 | Per-symbol single-print volume floors |
| `OPT_CLUSTER_WINDOW` | 5 | Cluster window (bars) |
| `OPT_CLUSTER_WINDOW_RATIO` | 3.0 | Cluster window-ratio threshold |
| `OPT_CLUSTER_ACTIVE_MIN` | 3 | Min active bars in the window |
| `NEAR_LOW_MAX_DIST` / `CONTRACT_LOW_MAX_DIST` | 1.75 / 2.50 | NearLow / TooChased (today's low) |
| `HIST_LOW_ENTRY_GATE` | true | Require actionable entries to be near the contract's multi-day historical low |
| `OPT_HIST_LOOKBACK_DAYS` | 10 | Days of Schwab daily candles for the historical low |
| `HIST_LOW_NEAR_RATIO` | 1.25 | Max `mark / hist_low` for an entry (else downgraded to WATCH) |
| `SINGLE_PRINT_RANKS` | {2,3} | Ranks eligible for MEDIUM_HIGH single print |
| `CLUSTER_UPGRADE_ENABLED` | true | Allow higher-confidence upgrades |
| `EMIT_UPGRADE_ALERT` | false | Whether an upgrade emits a 2nd same-direction alert (off ⇒ one call + one put per ticker) |
| `EMIT_WATCH_ONLY` | true | Emit non-qualifying prints as WATCH |
| `NEXT_DAY_MODE_ENABLED` | true | Tue/Thu interchangeable levels + OTM strike |
| `NEXT_DAY_TARGET_DEPTH` | 1 | Levels to step for the OTM strike |
| `LEVEL_FLIP_DEADBAND_PCT` | 0.0015 | Deadband before a level flips role |
| `EOD_CLOSE_NEXT_DAY` | true | Close next-day positions at EOD |
| `BAR_RETENTION_DAYS` | 10 | Trading days of 1-min data kept |
| `COLLECT_LEVEL_BARS` | true | Collect 1-min OHLCV for the 6 levels |
| `SESSION_BARS` / `BARS_TO_FETCH` | 400 / 40 | Full session vs detector slice |

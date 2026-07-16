"""
Central configuration — all tuneable parameters live here.
Override any value via the corresponding env var or .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Universe ─────────────────────────────────────────────────────────────────
SYMBOLS: list[str] = [
    'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA', 'TSLA',
]

MAG7: list[str] = ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA', 'TSLA']

# ── Benchmarks & relative strength ────────────────────────────────────────────
# SPY/QQQ are CONTEXT ONLY — tracked for price + relative strength, never traded
# and never run through the signal detector. MAG7 usually moves with QQQ; the point
# is to surface names moving relatively strong/weak INDEPENDENT of QQQ.
BENCHMARKS: list[str] = ['SPY', 'QQQ']
RELATIVE_STRENGTH_ENABLED = os.getenv('RELATIVE_STRENGTH_ENABLED', 'true').lower() == 'true'
RS_BENCHMARK              = os.getenv('RS_BENCHMARK', 'QQQ')          # MAG7 measured vs this
# Raw relative return: RS = stock %change - benchmark %change (percentage points).
# |RS| at/above the threshold flags a name as relatively strong / weak (a divergence).
RS_DIVERGENCE_PCT          = float(os.getenv('RS_DIVERGENCE_PCT',          '0.5'))  # morning (pre-mkt drift)
RS_INTRADAY_DIVERGENCE_PCT = float(os.getenv('RS_INTRADAY_DIVERGENCE_PCT', '0.4'))  # intraday (since open)
# Intraday: post a Discord note the first time a name crosses into a hard divergence.
RS_INTRADAY_DISCORD_ALERT  = os.getenv('RS_INTRADAY_DISCORD_ALERT', 'false').lower() == 'true'

# ── V2 adaptive watched-contract window + chain leadership ─────────────────────
# The old ATM±1 (n=3) window missed coordinated OTM chain moves (e.g. GOOGL 357.5-365C
# on 2026-07-14 — all outside the window, never evaluated). Widen the nearest-N call/put
# strikes fetched per poll, per underlying (fast movers reach farther), + a bonus for
# next-day (1DTE) flow which can position farther OTM. Gated — n=3 when off.
ADAPTIVE_WINDOW_ENABLED = os.getenv('ADAPTIVE_WINDOW_ENABLED', 'false').lower() == 'true'
CHAIN_WINDOW_N = {'default': 5, 'AAPL': 5, 'MSFT': 5, 'AMZN': 5,
                  'GOOGL': 6, 'META': 6, 'NVDA': 7, 'TSLA': 7}
CHAIN_WINDOW_NEXTDAY_BONUS = int(os.getenv('CHAIN_WINDOW_NEXTDAY_BONUS', '1'))
# Chain-leadership production path (measures cross-strike CALL/PUT control over the wider
# window, not single-strike thresholds). Needs the adaptive window. Default OFF (ships dark).
CHAIN_LEADERSHIP_ENABLED       = os.getenv('CHAIN_LEADERSHIP_ENABLED', 'false').lower() == 'true'
CHAIN_LEADERSHIP_STRIKE_MIN_VOL = int(os.getenv('CHAIN_LEADERSHIP_STRIKE_MIN_VOL', '200'))  # per-strike participation floor
CHAIN_LEADERSHIP_MIN_BREADTH    = int(os.getenv('CHAIN_LEADERSHIP_MIN_BREADTH', '3'))       # coordinated strikes
CHAIN_LEADERSHIP_MIN_COMBINED_VOL = int(os.getenv('CHAIN_LEADERSHIP_MIN_COMBINED_VOL', '1500'))
CHAIN_LEADERSHIP_MIN_NOTIONAL   = int(os.getenv('CHAIN_LEADERSHIP_MIN_NOTIONAL', '100000'))
CHAIN_LEADERSHIP_MARGIN         = float(os.getenv('CHAIN_LEADERSHIP_MARGIN', '1.5'))        # controlling side dominance
CHAIN_LEADERSHIP_CONVEXITY_FRAC = float(os.getenv('CHAIN_LEADERSHIP_CONVEXITY_FRAC', '0.4'))
CHAIN_LEADERSHIP_MIN_CONFIDENCE = int(os.getenv('CHAIN_LEADERSHIP_MIN_CONFIDENCE', '60'))
# Leadership is a momentum/breakout entry: the chain has ALREADY moved, so the value
# "near contract low" ratio (mark/session_low) is the wrong gate — a $0.28→$3 call reads
# as 11x "chased" yet that run IS the leadership. Instead of a value ratio, the only entry
# guard is an absolute premium floor (avoid dead sub-floor pennies); leadership confidence
# + breadth + notional carry the entry.
CHAIN_LEADERSHIP_MIN_PREMIUM    = float(os.getenv('CHAIN_LEADERSHIP_MIN_PREMIUM', '0.20'))

# ── Fresh-OI positioning engine (Engine 2 — overnight context, NEVER a trigger) ──
# Where institutions placed meaningful NEW risk since the prior session. Context/confidence
# only, so safe to default ON (it adds a briefing section + record; it never fires a trade).
POSITIONING_ENABLED          = os.getenv('POSITIONING_ENABLED', 'true').lower() == 'true'
POSITIONING_NEAR_BAND_PCT    = float(os.getenv('POSITIONING_NEAR_BAND_PCT', '0.05'))   # strikes within ±5% weighted
POSITIONING_TARGET_NOTIONAL  = int(os.getenv('POSITIONING_TARGET_NOTIONAL', '500000')) # fresh notional that saturates 'size'
POSITIONING_BUILD_MIN        = int(os.getenv('POSITIONING_BUILD_MIN', '250'))          # fresh OI = a BUILD
POSITIONING_UNWIND_MIN       = int(os.getenv('POSITIONING_UNWIND_MIN', '250'))         # OI drop = an UNWIND
POSITIONING_ROTATION_VOL_MIN = int(os.getenv('POSITIONING_ROTATION_VOL_MIN', '1000'))  # flat-OI + this vol = ROTATION
POSITIONING_FLAT_OI_MAX      = int(os.getenv('POSITIONING_FLAT_OI_MAX', '100'))

# ── Session timezone ──────────────────────────────────────────────────────────
SESSION_TZ = 'America/Chicago'

# ── Market schedule (all times in CST/CDT) ───────────────────────────────────
SNAPSHOT_HOUR   = int(os.getenv('SNAPSHOT_HOUR',   '8'))    # 08:10 CST — pre-market OI snapshot
SNAPSHOT_MINUTE = int(os.getenv('SNAPSHOT_MINUTE', '10'))
MARKET_OPEN_HOUR    = 8    # 08:30 CST = 09:30 ET
MARKET_OPEN_MINUTE  = 30
MARKET_CLOSE_HOUR   = 15   # 15:00 CST = 16:00 ET
MARKET_CLOSE_MINUTE = 0

# Warm-up window: for the first N minutes after open the detector still ingests
# bars and builds its volume baselines, but emits NO signals — this avoids acting
# on noisy opening prints before a baseline exists. Set 0 to disable.
SIGNAL_WARMUP_MINUTES = int(os.getenv('SIGNAL_WARMUP_MINUTES', '5'))

# ── OI level computation ──────────────────────────────────────────────────────
# Strikes considered "near ATM": ± ATM_RANGE_PCT of current price
ATM_RANGE_PCT  = float(os.getenv('ATM_RANGE_PCT', '0.05'))   # 5 %
TOP_N_LEVELS   = int(os.getenv('TOP_N_LEVELS', '3'))          # 3 S + 3 R

# §11-§16 Secondary OI Watchlist — three tiers beyond the primary S1-R3 levels.
SECONDARY_WATCHLIST_TOP_N  = int(os.getenv('SECONDARY_WATCHLIST_TOP_N',  '5'))    # extra ranks per side (S4-S8, R4-R8)
SECONDARY_OUTER_BAND_PCT   = float(os.getenv('SECONDARY_OUTER_BAND_PCT', '0.10')) # outer wall band ±10%
SECONDARY_OUTER_TOP_N      = int(os.getenv('SECONDARY_OUTER_TOP_N',      '3'))    # top-N outer-wall per side
SECONDARY_OI_BUILDUP_TOP_N = int(os.getenv('SECONDARY_OI_BUILDUP_TOP_N', '5'))   # top-N by overnight oi_change

# ── Weekend OI gaps ───────────────────────────────────────────────────────────
# On the first session after a multi-day market closure (Monday, or post-holiday)
# the OI change since the prior session spans the whole weekend. We snapshot the
# near-dated multi-expiry chain daily into near_oi_snapshots, then on that first
# session flag strikes whose OI jumped a lot over the gap. A strike qualifies only
# if it clears BOTH thresholds (abs floor removes noisy low-OI strikes), then the
# survivors are ranked biggest-first and the top-N reported per expiration.
WEEKEND_OI_GAPS_ENABLED  = os.getenv('WEEKEND_OI_GAPS_ENABLED', 'true').lower() == 'true'
WEEKEND_GAP_MIN_CONTRACTS = int(os.getenv('WEEKEND_GAP_MIN_CONTRACTS', '1000'))   # abs OI change floor
WEEKEND_GAP_MIN_PCT       = float(os.getenv('WEEKEND_GAP_MIN_PCT', '0.25'))       # 25% change floor
WEEKEND_GAP_TOP_N         = int(os.getenv('WEEKEND_GAP_TOP_N', '5'))             # top-N gaps reported
NEAR_OI_EXPIRY_DAYS       = int(os.getenv('NEAR_OI_EXPIRY_DAYS', '14'))          # this week + next

# Morning briefing: check Alpaca for any open option positions carried into the
# session (normally flat post-EOD; surfaces carryover/orphans). Read-only.
BRIEFING_CHECK_OPEN_POSITIONS = os.getenv('BRIEFING_CHECK_OPEN_POSITIONS', 'true').lower() == 'true'

# ── Signal detection ──────────────────────────────────────────────────────────
# Minimum minutes between signals on the same level (cooldown)
SIGNAL_COOLDOWN_MINUTES      = int(os.getenv('SIGNAL_COOLDOWN_MINUTES', '30'))

# Step 2: Tiered proximity bands — price must be within PROX_BAND_WIDE to be scored at all
PROX_BAND_TIGHT = float(os.getenv('PROX_BAND_TIGHT', '0.0025'))  # ≤0.25% → score 1.00
PROX_BAND_MID   = float(os.getenv('PROX_BAND_MID',   '0.0035'))  # ≤0.35% → score 0.70
PROX_BAND_WIDE  = float(os.getenv('PROX_BAND_WIDE',  '0.0050'))  # ≤0.50% → score 0.50

# Step 3: rolling baseline window for spike ratio computation
OPT_SPIKE_LOOKBACK = int(os.getenv('OPT_SPIKE_LOOKBACK', '10'))   # N prior bars for AvgVol

# Volume gate thresholds (legacy — retained for check_opposite_side / tuning)
OPT_SINGLE_SPIKE_RATIO  = float(os.getenv('OPT_SINGLE_SPIKE_RATIO', '5.0'))  # AbnormalBurst: 1-bar ratio ≥ 5×
OPT_CONSEC_SPIKE_RATIO  = float(os.getenv('OPT_CONSEC_SPIKE_RATIO', '3.0'))  # MultiBarCluster: window ratio ≥ 3×
OPT_EXTREME_SPIKE_RATIO = float(os.getenv('OPT_EXTREME_SPIKE_RATIO', '8.0'))  # ExtremeSingleStrike: ratio ≥ 8×

# Per-symbol-group absolute volume floors (AbnormalBurst and MultiBarCluster)
OPT_MIN_SPIKE_VOL   = {'TSLA': 250, 'NVDA': 250, 'default': 100}  # CurrentVol floor for AbnormalBurst
OPT_MIN_CLUSTER_VOL = {'TSLA': 600, 'NVDA': 600, 'default': 300}  # WindowVol floor for MultiBarCluster

# ── Single print vs cluster — spec logic ──────────────────────────────────────
# Baseline lookback: AvgPrior10MinVolume = average of up to N bars before the bar.
OPT_PRIOR_LOOKBACK = int(os.getenv('OPT_PRIOR_LOOKBACK', '10'))

# Valid single print: 1-min volume ≥ floor AND ratio ≥ 8× AND contract near lows.
OPT_SINGLE_PRINT_RATIO   = float(os.getenv('OPT_SINGLE_PRINT_RATIO', '8.0'))
OPT_MIN_SINGLE_PRINT_VOL = {'TSLA': 750, 'NVDA': 750, 'default': 300}

# ── Production volume gate (two-path) — §18 ───────────────────────────────────
# Volume is gated on ABSOLUTE size (ratio alone is NEVER sufficient) via two paths:
#   Path A DOMINANT ABSOLUTE — a very large event qualifies on size + concentration.
#   Path B CONTEXTUAL LEVEL CONVICTION — moderate size qualifies only with extreme
#     ratio + exact primary OI level + correct ATM/1-ITM contract + near contract low
#     + concentrated event + meaningful premium notional.
# Preserves gold-standard setups (e.g. NVDA 210P ≈ 500 contracts at R1, at its low,
# 45× ratio, ~$86k notional) while blocking small-volume / huge-ratio spam.
TRUE_CONVICTION_GATE_ENABLED        = os.getenv('TRUE_CONVICTION_GATE_ENABLED', 'true').lower() == 'true'
CONTEXTUAL_LEVEL_CONVICTION_ENABLED = os.getenv('CONTEXTUAL_LEVEL_CONVICTION_ENABLED', 'true').lower() == 'true'

# Path B base floors (moderate absolute volume — also the spam floor, §6).
SINGLE_PRINT_BASE_FLOOR = int(os.getenv('SINGLE_PRINT_BASE_FLOOR', '500'))    # peak 1-min
THREE_MINUTE_BASE_FLOOR = int(os.getenv('THREE_MINUTE_BASE_FLOOR', '1000'))   # 3-min window
FIVE_MINUTE_BASE_FLOOR  = int(os.getenv('FIVE_MINUTE_BASE_FLOOR',  '1250'))   # 5-min window

# ── Mandatory production volume floors (tightened) ────────────────────────────
# A production event (primary-level OR chain-led, call OR put) must clear at least
# one absolute floor: peak_1m >= PEAK_1M_VOLUME_MIN OR volume_3m >= VOLUME_3M_MIN.
# These are BINDING: a high ratio or the dominant-absolute path never overrides
# them. Stricter during the first 15 minutes after the open. Below-floor events are
# never traded — they are stored research-only (RESEARCH_ONLY_SUBTHRESHOLD_EVENT)
# and still outcome-scored. Env-overridable so the floors can be reverted live.
PEAK_1M_VOLUME_MIN         = int(os.getenv('PEAK_1M_VOLUME_MIN',         '1000'))
VOLUME_3M_MIN              = int(os.getenv('VOLUME_3M_MIN',              '2000'))
OPENING_PEAK_1M_VOLUME_MIN = int(os.getenv('OPENING_PEAK_1M_VOLUME_MIN', '1250'))
OPENING_VOLUME_3M_MIN      = int(os.getenv('OPENING_VOLUME_3M_MIN',      '2500'))

# ── Gold-only production mode (§ Gold spec / July-1 patch) — P1 foundation ────
# MASTER SWITCH. When true, only Gold-classified events create Discord alerts +
# paper trades; everything else is stored research-only (§19). Default FALSE:
# production behavior is unchanged until the control-test suite (P6) passes and it
# is deliberately enabled. All values env-overridable.
GOLD_ONLY_PRODUCTION_MODE             = os.getenv('GOLD_ONLY_PRODUCTION_MODE', 'false').lower() == 'true'
GOLD_PRIMARY_ENABLED                  = os.getenv('GOLD_PRIMARY_ENABLED', 'true').lower() == 'true'
GOLD_CHAIN_LED_ENABLED                = os.getenv('GOLD_CHAIN_LED_ENABLED', 'true').lower() == 'true'
PRIMARY_CHAIN_MERGE_ENABLED           = os.getenv('PRIMARY_CHAIN_MERGE_ENABLED', 'true').lower() == 'true'
INTENT_VALIDATION_ENABLED             = os.getenv('INTENT_VALIDATION_ENABLED', 'true').lower() == 'true'
INTENT_CONFIRMATION_BARS_MIN          = int(os.getenv('INTENT_CONFIRMATION_BARS_MIN', '1'))
INTENT_CONFIRMATION_BARS_MAX          = int(os.getenv('INTENT_CONFIRMATION_BARS_MAX', '3'))
OPPOSITE_SIDE_VETO_ENABLED            = os.getenv('OPPOSITE_SIDE_VETO_ENABLED', 'true').lower() == 'true'
OPENING_FULL_CHAIN_SCAN_ENABLED       = os.getenv('OPENING_FULL_CHAIN_SCAN_ENABLED', 'true').lower() == 'true'
HISTORICAL_VALUE_REGION_MODEL_ENABLED = os.getenv('HISTORICAL_VALUE_REGION_MODEL_ENABLED', 'true').lower() == 'true'
SAME_DIRECTION_UPGRADE_ENABLED        = os.getenv('SAME_DIRECTION_UPGRADE_ENABLED', 'true').lower() == 'true'
MAX_SAME_DIRECTION_UPGRADES_PER_DAY   = int(os.getenv('MAX_SAME_DIRECTION_UPGRADES_PER_DAY', '1'))
COUNTERTREND_STRICT_MODE              = os.getenv('COUNTERTREND_STRICT_MODE', 'true').lower() == 'true'
ESTABLISHED_MOVE_PCT                  = float(os.getenv('ESTABLISHED_MOVE_PCT', '0.01'))
LEADERSHIP_FADE_RATIO                 = float(os.getenv('LEADERSHIP_FADE_RATIO', '0.50'))
FRESH_CONVICTION_LOOKBACK_MIN         = int(os.getenv('FRESH_CONVICTION_LOOKBACK_MIN', '10'))
TREND_PROGRESS_LOOKBACK_BARS          = int(os.getenv('TREND_PROGRESS_LOOKBACK_BARS', '5'))
GOLD_EXCEPTIONAL_SINGLE_1M            = int(os.getenv('GOLD_EXCEPTIONAL_SINGLE_1M', '2000'))  # Route B (P3)
GOLD_MIN_PREMIUM_NOTIONAL             = int(os.getenv('GOLD_MIN_PREMIUM_NOTIONAL', '0'))      # 0 = reuse existing notional gate until tuned
# Historical-value regions (§12) — percentile upper bounds
HV_REGION_EXCELLENT_MAX  = float(os.getenv('HV_REGION_EXCELLENT_MAX',  '0.25'))
HV_REGION_ACCEPTABLE_MAX = float(os.getenv('HV_REGION_ACCEPTABLE_MAX', '0.45'))
HV_REGION_NEUTRAL_MAX    = float(os.getenv('HV_REGION_NEUTRAL_MAX',    '0.65'))
# Contract-low-distance regions (§13) — upper bounds
CLOW_GOLD_MAX       = float(os.getenv('CLOW_GOLD_MAX',       '1.25'))
CLOW_STRONG_MAX     = float(os.getenv('CLOW_STRONG_MAX',     '1.50'))
CLOW_ACCEPTABLE_MAX = float(os.getenv('CLOW_ACCEPTABLE_MAX', '1.75'))
# Directional-intent validation (§5-§9, P2) tolerances
INTENT_PREMIUM_HOLD_PCT   = float(os.getenv('INTENT_PREMIUM_HOLD_PCT',   '-0.10'))  # premium may dip ≤10% and still "hold"
INTENT_SPOT_CONTRADICT_PCT= float(os.getenv('INTENT_SPOT_CONTRADICT_PCT', '0.003')) # spot move that counts as contradicting the thesis
LEADERSHIP_VETO_MARGIN    = float(os.getenv('LEADERSHIP_VETO_MARGIN',     '0.15'))  # opposite side must lead by this to veto
# Event-time capture (P-ET) — freeze ATM/spot/quotes at the threshold-cross instant so
# strike eligibility uses the state WHEN flow occurred, not at bar-close (fixes fast
# opening moves running away from the initiating contract). Default off until wired.
EVENT_TIME_ELIGIBILITY_ENABLED = os.getenv('EVENT_TIME_ELIGIBILITY_ENABLED', 'false').lower() == 'true'
OPENING_STRIKE_WINDOW          = int(os.getenv('OPENING_STRIKE_WINDOW',          '5'))   # ATM ± N strikes in the opening 15m
OPENING_EVENT_WATCH_VOLUME     = int(os.getenv('OPENING_EVENT_WATCH_VOLUME',     '500'))  # r60 that registers a watch event
OPENING_EVENT_CONTRACT_TTL_MIN = int(os.getenv('OPENING_EVENT_CONTRACT_TTL_MIN', '30'))   # keep a registered contract alive this long
# Fix (2), Option C — promote opening-window event-time-eligible contracts to PRODUCTION
# (not just research logging). A candidate fires only when the both-sided opening story is
# demand-dominant on its side, priced at commit time (no retrospective qualification), and
# it clears the full chain-led/Route-B economic + veto + Gold gates. Default OFF — ships
# dark; validate on replay before enabling.
OPENING_SCAN_PRODUCTION_ENABLED = os.getenv('OPENING_SCAN_PRODUCTION_ENABLED', 'false').lower() == 'true'

# Breakout/breakdown continuation (P-BD) — primary levels also produce continuation
# when price ACCEPTS through them (resistance->CALL breakout, support->PUT breakdown),
# not only bounces/rejections. Acceptance = a completed bar beyond the level, OR beyond
# by max(BREAKOUT_LEVEL_BUFFER_ABS, level*BREAKOUT_LEVEL_BUFFER_PCT). Default off.
BREAKOUT_BREAKDOWN_ENABLED = os.getenv('BREAKOUT_BREAKDOWN_ENABLED', 'false').lower() == 'true'
BREAKOUT_ACCEPTANCE_BARS   = int(os.getenv('BREAKOUT_ACCEPTANCE_BARS', '1'))
BREAKOUT_LEVEL_BUFFER_PCT  = float(os.getenv('BREAKOUT_LEVEL_BUFFER_PCT', '0.001'))
BREAKOUT_LEVEL_BUFFER_ABS  = float(os.getenv('BREAKOUT_LEVEL_BUFFER_ABS', '0.25'))

# Path A dominant floors (per-symbol — NVDA/TSLA trade heavier).
DOMINANT_SINGLE_PRINT = {'NVDA': 1000, 'TSLA': 1000, 'default': 750}
DOMINANT_3M           = {'NVDA': 1750, 'TSLA': 1750, 'default': 1250}
DOMINANT_5M           = {'NVDA': 2500, 'TSLA': 2500, 'default': 1750}

# Relative volume (Path B context only — never sufficient on its own).
CONTEXTUAL_SINGLE_PRINT_RATIO = float(os.getenv('CONTEXTUAL_SINGLE_PRINT_RATIO', '8.0'))
CONTEXTUAL_MULTI_BAR_RATIO    = float(os.getenv('CONTEXTUAL_MULTI_BAR_RATIO',    '3.0'))

# Event-concentration share by shape (§10) — share of recent window taken by the event.
SINGLE_PRINT_EVENT_SHARE_MIN = float(os.getenv('SINGLE_PRINT_EVENT_SHARE_MIN', '0.35'))
THREE_MINUTE_EVENT_SHARE_MIN = float(os.getenv('THREE_MINUTE_EVENT_SHARE_MIN', '0.40'))
FIVE_MINUTE_EVENT_SHARE_MIN  = float(os.getenv('FIVE_MINUTE_EVENT_SHARE_MIN',  '0.45'))
DOMINANT_EVENT_SHARE_MIN     = float(os.getenv('DOMINANT_EVENT_SHARE_MIN',     '0.45'))

# Premium notional floor (§11): TriggerVolume × OptionMark × 100.
MINIMUM_PREMIUM_NOTIONAL_0DTE        = int(os.getenv('MINIMUM_PREMIUM_NOTIONAL_0DTE',        '50000'))
MINIMUM_PREMIUM_NOTIONAL_NEXT_EXPIRY = int(os.getenv('MINIMUM_PREMIUM_NOTIONAL_NEXT_EXPIRY', '75000'))

# Path B contract-value location (§4E): hard ≤ 1.50, preferred (gold-standard) ≤ 1.25.
CONTEXTUAL_LOW_DIST_MAX = float(os.getenv('CONTEXTUAL_LOW_DIST_MAX', '1.50'))
GOLD_STANDARD_LOW_DIST  = float(os.getenv('GOLD_STANDARD_LOW_DIST',  '1.25'))

# Partial-bar pending (§7-8): hold a candidate whose partial volume is within this
# fraction of the floor and re-evaluate on the completed 1-min bar.
PENDING_VOLUME_TOLERANCE_PCT = float(os.getenv('PENDING_VOLUME_TOLERANCE_PCT', '0.20'))

# Chain evidence (§12) — computed and logged, NOT a hard gate by default.
CHAIN_DOMINANCE_HARD_GATE_ENABLED = os.getenv('CHAIN_DOMINANCE_HARD_GATE_ENABLED', 'false').lower() == 'true'

# ── Chain-led emergent entry path (§20) ───────────────────────────────────────
# Allow CALL/PUT alerts when coordinated ATM + adjacent-strike volume builds a new
# emergent support/resistance BEFORE spot reaches a morning OI level. Additive to the
# primary-level path; requires stronger multi-strike evidence instead of proximity.
CHAIN_LED_ENTRY_ENABLED           = os.getenv('CHAIN_LED_ENTRY_ENABLED', 'true').lower() == 'true'
CHAIN_CONFIRMATION_WINDOW_MINUTES = int(os.getenv('CHAIN_CONFIRMATION_WINDOW_MINUTES', '5'))
# Combined volume across {1 ITM, ATM, 1 OTM} of the confirm side (§4C / §5).
CHAIN_CALL_COMBINED_1M_FLOOR = int(os.getenv('CHAIN_CALL_COMBINED_1M_FLOOR', '1000'))
CHAIN_CALL_COMBINED_3M_FLOOR = int(os.getenv('CHAIN_CALL_COMBINED_3M_FLOOR', '1500'))
CHAIN_CALL_COMBINED_5M_FLOOR = int(os.getenv('CHAIN_CALL_COMBINED_5M_FLOOR', '2000'))
CHAIN_PUT_COMBINED_1M_FLOOR  = int(os.getenv('CHAIN_PUT_COMBINED_1M_FLOOR', '1000'))
CHAIN_PUT_COMBINED_3M_FLOOR  = int(os.getenv('CHAIN_PUT_COMBINED_3M_FLOOR', '1500'))
CHAIN_PUT_COMBINED_5M_FLOOR  = int(os.getenv('CHAIN_PUT_COMBINED_5M_FLOOR', '2000'))
# Individual-strike quality (§4D): ATM and adjacent floors.
CHAIN_ATM_1M_MIN      = int(os.getenv('CHAIN_ATM_1M_MIN', '500'))
CHAIN_ATM_3M_MIN      = int(os.getenv('CHAIN_ATM_3M_MIN', '1000'))
CHAIN_ADJACENT_1M_MIN = int(os.getenv('CHAIN_ADJACENT_1M_MIN', '350'))
CHAIN_ADJACENT_3M_MIN = int(os.getenv('CHAIN_ADJACENT_3M_MIN', '700'))
# Economic size (§4F), contract-value location (§4E/§4J), leadership (§4I), concentration (§4G).
CHAIN_COMBINED_NOTIONAL_MIN     = int(os.getenv('CHAIN_COMBINED_NOTIONAL_MIN', '100000'))
CHAIN_ATM_NOTIONAL_MIN          = int(os.getenv('CHAIN_ATM_NOTIONAL_MIN', '50000'))
CHAIN_ATM_LOW_DISTANCE_MAX      = float(os.getenv('CHAIN_ATM_LOW_DISTANCE_MAX', '1.50'))
CHAIN_ADJACENT_LOW_DISTANCE_MAX = float(os.getenv('CHAIN_ADJACENT_LOW_DISTANCE_MAX', '1.75'))
CHAIN_SELECTED_LOW_DISTANCE_MAX = float(os.getenv('CHAIN_SELECTED_LOW_DISTANCE_MAX', '1.75'))
CHAIN_LEADERSHIP_MIN           = float(os.getenv('CHAIN_LEADERSHIP_MIN', '0.75'))
CHAIN_LEADERSHIP_MARGIN        = float(os.getenv('CHAIN_LEADERSHIP_MARGIN', '0.20'))
CHAIN_EVENT_SHARE_MIN          = float(os.getenv('CHAIN_EVENT_SHARE_MIN', '0.35'))
CHAIN_COMBINED_EVENT_SHARE_MIN = float(os.getenv('CHAIN_COMBINED_EVENT_SHARE_MIN', '0.45'))

# ── Intraday trend tracker + countertrend reversal gate (§8-14, §20) ───────────
# A signal opposing a strong, still-working, leadership-confirmed move must clear a
# STRICTER gate than an ordinary continuation entry, else it is held as a watch.
COUNTERTREND_GATE_ENABLED = os.getenv('COUNTERTREND_GATE_ENABLED', 'true').lower() == 'true'
# Trend model (lightweight: established move% + leadership; VWAP is not a gate).
ESTABLISHED_MOVE_PCT          = float(os.getenv('ESTABLISHED_MOVE_PCT', '0.01'))   # |spot−open|/open
LEADERSHIP_FADE_RATIO         = float(os.getenv('LEADERSHIP_FADE_RATIO', '0.50'))  # ≤ this × session peak = fading
FRESH_CONVICTION_LOOKBACK_MIN = int(os.getenv('FRESH_CONVICTION_LOOKBACK_MIN', '10'))
TREND_PROGRESS_LOOKBACK_BARS  = int(os.getenv('TREND_PROGRESS_LOOKBACK_BARS', '5'))  # new high/low within N bars = working
# Stricter countertrend absolute floors (§9) — per-symbol.
COUNTERTREND_SINGLE_PRINT_FLOOR = {'NVDA': 1250, 'TSLA': 1250, 'default': 1000}
COUNTERTREND_3M_FLOOR           = {'NVDA': 2250, 'TSLA': 2250, 'default': 1750}
COUNTERTREND_5M_FLOOR           = {'NVDA': 3000, 'TSLA': 3000, 'default': 2500}
# Stricter countertrend leadership (§11).
COUNTERTREND_LEADERSHIP_MIN    = float(os.getenv('COUNTERTREND_LEADERSHIP_MIN', '0.80'))
COUNTERTREND_LEADERSHIP_MARGIN = float(os.getenv('COUNTERTREND_LEADERSHIP_MARGIN', '0.25'))
# §14 watch window — hold a sub-threshold countertrend event this long for promotion.
COUNTERTREND_WATCH_MINUTES     = int(os.getenv('COUNTERTREND_WATCH_MINUTES', '30'))

# Valid volume cluster: rolling 5-bar window.
#   WindowRatio5 = WindowVol5 / (window * max(AvgPrior10, 10))  ≥ 3.0
#   ActiveBars5  = bars in window with per-bar ratio ≥ 2.0      ≥ 3
#   BurstBars5   = bars in window with per-bar ratio ≥ 4.0      (informational)
OPT_CLUSTER_WINDOW       = int(os.getenv('OPT_CLUSTER_WINDOW', '5'))
OPT_CLUSTER_WINDOW_RATIO = float(os.getenv('OPT_CLUSTER_WINDOW_RATIO', '3.0'))
OPT_CLUSTER_ACTIVE_RATIO = float(os.getenv('OPT_CLUSTER_ACTIVE_RATIO', '2.0'))
OPT_CLUSTER_ACTIVE_MIN   = int(os.getenv('OPT_CLUSTER_ACTIVE_MIN', '3'))
OPT_CLUSTER_BURST_RATIO  = float(os.getenv('OPT_CLUSTER_BURST_RATIO', '4.0'))

# Extreme single prints rank as MEDIUM_HIGH only at S2/S3 or R2/R3 (ranks 2,3).
SINGLE_PRINT_RANKS = {2, 3}

# Emit non-qualifying (but notable) prints as WATCH_ONLY instead of discarding.
EMIT_WATCH_ONLY = os.getenv('EMIT_WATCH_ONLY', 'true').lower() == 'true'

# Allow a later, strictly higher-confidence signal (e.g. ATM_ITM_CLUSTER) to
# supersede an earlier single-print alert in the same direction. The upgrade
# fires as a fresh alert but is not auto-traded (the original already entered).
CLUSTER_UPGRADE_ENABLED = os.getenv('CLUSTER_UPGRADE_ENABLED', 'true').lower() == 'true'
# Whether a cluster upgrade emits a SECOND same-direction alert. Off by default so
# each ticker yields at most one call and one put symbol; the upgrade still updates
# internal state but won't add another message.
EMIT_UPGRADE_ALERT = os.getenv('EMIT_UPGRADE_ALERT', 'false').lower() == 'true'

# ── Next-day-expiry mode (Tue/Thu — no 0DTE) ──────────────────────────────────
# When today has no same-day expiry, the nearest expiry is next-day. In that
# mode S/R levels are interchangeable (a level's role is set by spot position
# each bar) and the traded strike is the ATM strike at the *target* level
# (OTM relative to spot). 0DTE days (Mon/Wed/Fri) are unaffected. EOD close
# behaviour is unchanged.
NEXT_DAY_MODE_ENABLED = os.getenv('NEXT_DAY_MODE_ENABLED', 'true').lower() == 'true'
# How many levels toward the target to step for the OTM strike (1 = nearest).
NEXT_DAY_TARGET_DEPTH = int(os.getenv('NEXT_DAY_TARGET_DEPTH', '1'))
# Deadband around a level before its role flips, to avoid whipsaw right at the
# strike. Spot must be more than this fraction beyond the strike to flip role.
LEVEL_FLIP_DEADBAND_PCT = float(os.getenv('LEVEL_FLIP_DEADBAND_PCT', '0.0015'))
# Close next-day-expiry positions at EOD too (choice: keep EOD close, no overnight).
EOD_CLOSE_NEXT_DAY = os.getenv('EOD_CLOSE_NEXT_DAY', 'true').lower() == 'true'

# Step 8: Contract low distance thresholds
# NearLow  (≤ 1.75×) required for all non-extreme signals
# TooChased (> 2.50×) hard block regardless
NEAR_LOW_MAX_DIST     = float(os.getenv('NEAR_LOW_MAX_DIST',     '1.75'))
CONTRACT_LOW_MAX_DIST = float(os.getenv('CONTRACT_LOW_MAX_DIST', '2.50'))

# ── Historical-low entry gate ──────────────────────────────────────────────────
# "For entry and alerts look for the historical low for that option": before an
# actionable entry fires, the contract we'd actually buy must be trading at/near
# its multi-day historical low (not merely near today's low). The contract's
# historical low is pulled from Schwab daily candles over OPT_HIST_LOOKBACK_DAYS,
# fetched once per contract per day and cached.
#
# NOTE: 0DTE contracts (Mon/Wed/Fri) have no prior-day history, so the gate is a
# no-op those days and only bites in next-day mode (Tue/Thu). When no history is
# available the entry is allowed (the existing today-low NearLow/TooChased gates
# still apply). A failing entry is downgraded to a WATCH alert, never silently
# dropped — so it still surfaces, it just isn't auto-traded.
HIST_LOW_ENTRY_GATE    = os.getenv('HIST_LOW_ENTRY_GATE', 'true').lower() == 'true'
OPT_HIST_LOOKBACK_DAYS = int(os.getenv('OPT_HIST_LOOKBACK_DAYS', '10'))
# Actionable entry requires mark / historical_low <= this ratio (≤25% above low).
HIST_LOW_NEAR_RATIO    = float(os.getenv('HIST_LOW_NEAR_RATIO', '1.25'))


# ══════════════════════════════════════════════════════════════════════════════
# SIMPLIFIED V1 ENTRY LOGIC  (Mag-7 call/put alert engine)
# These parameters drive analysis/signal_detector.py. Several legacy params above
# (PROX_BAND_*, SINGLE_PRINT_RANKS, MAX_SPREAD_PCT, TARGET_ROOM_*, *_UPGRADE_*,
# LEVEL_FLIP_DEADBAND_PCT, HIST_LOW_NEAR_RATIO) are no longer used by the detector
# under V1 — they are retained only so older test_*.py imports keep working.
# ══════════════════════════════════════════════════════════════════════════════

# Symbols treated as high-volatility (wider proximity, higher absolute floors).
VOLATILE_SYMBOLS = {'TSLA', 'NVDA'}

# §3 Premarket OI levels — valid strikes within ±OI_LEVEL_BAND_PCT of spot,
# ranked by OI, top 3 per side (R1/R2/R3 calls above, S1/S2/S3 puts below).
OI_LEVEL_BAND_PCT = float(os.getenv('OI_LEVEL_BAND_PCT', '0.05'))   # 5%

# §4 Level proximity — NearLevel if abs(spot-level)/spot <= distance. Binary.
NEAR_LEVEL_DIST_DEFAULT  = float(os.getenv('NEAR_LEVEL_DIST_DEFAULT',  '0.0035'))
NEAR_LEVEL_DIST_VOLATILE = float(os.getenv('NEAR_LEVEL_DIST_VOLATILE', '0.0050'))

# §10 Volume cluster — absolute WindowVol5 floor (reuses the existing per-symbol
# map, which already holds the spec's 600/600/300). WindowRatio5 / ActiveBars5
# thresholds come from OPT_CLUSTER_WINDOW_RATIO / OPT_CLUSTER_ACTIVE_* above.
OPT_MIN_CLUSTER_WINDOW_VOL = OPT_MIN_CLUSTER_VOL

# §11 Stair-step accumulation — weighted excitation over the last 5 ratios.
#   ExcitationRaw   = Σ STAIRSTEP_WEIGHTS[i] * VolumeRatio[t-i]
#   ExcitationScore = min(ExcitationRaw, 10)/10
#   valid if score >= min AND WindowRatio5 >= ratio_min AND ActiveBars5 >= active
#          AND ContractLowDistance <= low_dist_max
STAIRSTEP_WEIGHTS          = (1.00, 0.60, 0.35, 0.20, 0.10)
STAIRSTEP_EXCITATION_MIN   = float(os.getenv('STAIRSTEP_EXCITATION_MIN',   '0.70'))
STAIRSTEP_WINDOW_RATIO_MIN = float(os.getenv('STAIRSTEP_WINDOW_RATIO_MIN', '2.5'))
STAIRSTEP_ACTIVE_MIN       = int(os.getenv('STAIRSTEP_ACTIVE_MIN', '3'))
STAIRSTEP_LOW_DIST_MAX     = float(os.getenv('STAIRSTEP_LOW_DIST_MAX', '2.0'))

# §13 Historical value percentile — (mark-HistLow)/(HistHigh-HistLow) over the
# FULL stored option history (all prior sessions). Require the contract at/near
# its relative historical low: block unless it sits in the bottom third of range.
HIST_VALUE_PCTILE_MAX = float(os.getenv('HIST_VALUE_PCTILE_MAX', '0.33'))

# §14 Short-cover risk — a fresh major volume event that mirrors an earlier event
# of similar size but at a much lower price looks like shorts covering, not new
# longs. Block when SimilarVolume AND RepriceRatio <= reprice_max.
SHORT_COVER_FILTER     = os.getenv('SHORT_COVER_FILTER', 'true').lower() == 'true'
SHORT_COVER_SIM_LOW    = float(os.getenv('SHORT_COVER_SIM_LOW',    '0.70'))
SHORT_COVER_SIM_HIGH   = float(os.getenv('SHORT_COVER_SIM_HIGH',   '1.50'))
SHORT_COVER_REPRICE_MAX = float(os.getenv('SHORT_COVER_REPRICE_MAX', '0.50'))

# §15 Opening range — first N minutes after open. Not blocked, but entries need
# stronger evidence: single-print floor ×mult, cluster WindowRatio5 >= ratio,
# stair-step ExcitationScore >= excitation.
OPENING_RANGE_MINUTES        = int(os.getenv('OPENING_RANGE_MINUTES', '15'))
OPENING_RANGE_VOL_MULT       = float(os.getenv('OPENING_RANGE_VOL_MULT', '1.5'))
OPENING_RANGE_CLUSTER_RATIO  = float(os.getenv('OPENING_RANGE_CLUSTER_RATIO', '4.0'))
OPENING_RANGE_EXCITATION_MIN = float(os.getenv('OPENING_RANGE_EXCITATION_MIN', '0.80'))

# Exit-target shift — the nearest opposite level is usually too close, so skip it:
#   CALL entered at support    → Exit1 = R2, Exit2 = R3  (skip R1)
#   PUT  entered at resistance → Exit1 = S2, Exit2 = S3  (skip S1)
# Fall back to the nearest (R1/S1) only when no farther level exists. After the
# shift, drop any target still within EXIT_MIN_ROOM_PCT of the entry spot.
EXIT_MIN_ROOM_PCT = float(os.getenv('EXIT_MIN_ROOM_PCT', '0.0025'))   # 0.25%

# ── Chandelier trailing exit (trail the runner) ───────────────────────────────
# After Exit1 banks the first half at level 1, the remaining half is trailed with a
# chandelier stop on the UNDERLYING instead of the fixed Exit2 level: the trail sits
# ATR*mult below the highest high since entry (mirror for puts) and ratchets up (never
# loosens); the runner stops out when the underlying reverses through it. Lets winners
# run into the fat tail while protecting the open gain. Default OFF — validate on the
# counterfactual replay before enabling.
CHANDELIER_EXIT_ENABLED = os.getenv('CHANDELIER_EXIT_ENABLED', 'false').lower() == 'true'
CHANDELIER_ATR_PERIOD   = int(os.getenv('CHANDELIER_ATR_PERIOD', '14'))   # 1-min bars
CHANDELIER_ATR_MULT     = float(os.getenv('CHANDELIER_ATR_MULT', '3.0'))  # classic 3x ATR

# ClusterStrength minimum thresholds by level rank (enforced gate, not informational)
CS_THRESHOLD_RANK1 = float(os.getenv('CS_THRESHOLD_RANK1', '0.80'))  # S1/R1
CS_THRESHOLD_RANK2 = float(os.getenv('CS_THRESHOLD_RANK2', '0.70'))  # S2/R2
CS_THRESHOLD_RANK3 = float(os.getenv('CS_THRESHOLD_RANK3', '0.65'))  # S3/R3

# Step 9: Spread filter — block if (ask-bid)/mid > 50%
MAX_SPREAD_PCT = float(os.getenv('MAX_SPREAD_PCT', '0.50'))

# ── Flow Leadership Reversal Engine (V1) ──────────────────────────────────────
# After a position opens, watch the OPPOSITE side; if it produces a concentrated
# volume event (burst out of a quiet background, contract near its low) while the
# same side fades, exit the position + alert. V1 does NOT auto-open the opposite
# trade — it closes the current (paper) position, alerts, and records the
# hypothetical opposite entry for measurement (spec §14/§19).
# DISABLED (2026-07-06): the reversal engine flipped into near-worthless far-OTM
# penny options (e.g. NVDA 200C @ $0.01, qty 860) and is not useful at this time.
# Turned off by default — this gates the ENTIRE reversal chain in main.py
# (evaluate -> _handle_reversal -> _flip_entry -> send_reversal_alert). The shared
# volume_event() helper in flow_reversal.py stays (the detector's volume gate uses
# it). Set FLOW_REVERSAL_ENABLED=true to re-enable.
FLOW_REVERSAL_ENABLED   = os.getenv('FLOW_REVERSAL_ENABLED', 'false').lower() == 'true'
# Auto-flip: on a confirmed reversal, OPEN the opposite paper trade. Off by default
# (and moot while FLOW_REVERSAL_ENABLED is false).
FLOW_REVERSAL_AUTO_FLIP = os.getenv('FLOW_REVERSAL_AUTO_FLIP', 'false').lower() == 'true'
REVERSAL_MAX_PER_DAY    = int(os.getenv('REVERSAL_MAX_PER_DAY', '3'))  # per-symbol flip cap (anti-churn)
REVERSAL_BURST_RATIO    = float(os.getenv('REVERSAL_BURST_RATIO', '3.0'))   # EventAvg / PreEventVol
REVERSAL_EVENT_SHARE    = float(os.getenv('REVERSAL_EVENT_SHARE', '0.40'))  # 5-bar / 20-bar volume
REVERSAL_ACTIVE_BARS    = int(os.getenv('REVERSAL_ACTIVE_BARS', '2'))       # bars >= 2x PreEventVol
REVERSAL_NEAR_LOW_MAX   = float(os.getenv('REVERSAL_NEAR_LOW_MAX', '1.75')) # contract-low qualifier
REVERSAL_WINDOW_MIN     = int(os.getenv('REVERSAL_WINDOW_MIN', '15'))       # flow-transition window
REVERSAL_FADE_WINDOW_MIN= int(os.getenv('REVERSAL_FADE_WINDOW_MIN', '10'))  # same-side fade lookback
REVERSAL_FADE_RATIO     = float(os.getenv('REVERSAL_FADE_RATIO', '0.50'))   # vol <= 0.5x peak = fading
REVERSAL_DOMINANT_BURST = float(os.getenv('REVERSAL_DOMINANT_BURST', '5.0'))
REVERSAL_DOMINANT_SHARE = float(os.getenv('REVERSAL_DOMINANT_SHARE', '0.60'))
REVERSAL_LEADERSHIP_MIN = float(os.getenv('REVERSAL_LEADERSHIP_MIN', '0.75'))  # opp leadership floor
REVERSAL_LEADERSHIP_DIFF= float(os.getenv('REVERSAL_LEADERSHIP_DIFF', '0.20')) # opp - same
# V2 control-exit confirmation layers (§ ownership change). These bind the reversal-
# confirmed exit ONLY when FLOW_REVERSAL_ENABLED is on, and are the fix for why the
# engine flipped into penny options: flow alone declared a reversal with nothing checking
# that the taking-control side's PREMIUM was actually expanding or that PRICE validated it.
#   Premium: the opposite (taking-control) side's premium must expand during the takeover.
#   Price:   the underlying must confirm — VWAP loss for a call position, VWAP reclaim for
#            a put position (price moving against the held side).
REVERSAL_PREMIUM_CONFIRM_ENABLED = os.getenv('REVERSAL_PREMIUM_CONFIRM_ENABLED', 'true').lower() == 'true'
REVERSAL_PREMIUM_EXPANSION_PCT   = float(os.getenv('REVERSAL_PREMIUM_EXPANSION_PCT', '0.05'))  # +5% from streak low (tuned: blocks all penny flips at 0% expansion, keeps AMZN +6%)
REVERSAL_PRICE_CONFIRM_ENABLED   = os.getenv('REVERSAL_PRICE_CONFIRM_ENABLED', 'true').lower() == 'true'

# Step 10: Target room thresholds (nearest opposing level distance from spot)
TARGET_ROOM_HIGH = float(os.getenv('TARGET_ROOM_HIGH', '0.0075'))  # score 1.00 (≥0.75%)
TARGET_ROOM_MID  = float(os.getenv('TARGET_ROOM_MID',  '0.0050'))  # score 0.70 (≥0.50%)
TARGET_ROOM_LOW  = float(os.getenv('TARGET_ROOM_LOW',  '0.0025'))  # score 0.40 (≥0.25%)

# P/C conviction multiplier thresholds (mirrors sentiment.py cutoffs)
PC_BULL_CUTOFF = float(os.getenv('PC_BULL_CUTOFF', '0.85'))   # below → BULLISH bias
PC_BEAR_CUTOFF = float(os.getenv('PC_BEAR_CUTOFF', '1.15'))   # above → BEARISH bias

# Legacy equity-bar parameters (kept for price_bars storage, not used in signal logic)
VOLUME_SPIKE_MULTIPLIER      = float(os.getenv('VOLUME_SPIKE_MULTIPLIER', '2.0'))
VOLUME_LOOKBACK_BARS         = int(os.getenv('VOLUME_LOOKBACK_BARS', '20'))
BARS_TO_FETCH                = int(os.getenv('BARS_TO_FETCH', '40'))
# Full-session cap for per-minute persistence (a RTH day has <=390 1-min bars).
SESSION_BARS                 = int(os.getenv('SESSION_BARS', '400'))
# Collect 1-min OHLCV for the 6 S/R level option contracts each poll (Schwab).
COLLECT_LEVEL_BARS           = os.getenv('COLLECT_LEVEL_BARS', 'true').lower() == 'true'
# Morning-snapshot 1-hour option-bar pull (Alpaca) for the 6 S/R level contracts.
# Runs once per day alongside the OI briefing; persists OPT_HOURLY_LOOKBACK_DAYS of
# 1Hour OHLCV per contract to option_hourly_bars for historical context/backtests.
COLLECT_HOURLY_OPTION_BARS   = os.getenv('COLLECT_HOURLY_OPTION_BARS', 'true').lower() == 'true'
OPT_HOURLY_LOOKBACK_DAYS     = int(os.getenv('OPT_HOURLY_LOOKBACK_DAYS', '10'))
# Retention: keep only this many recent trading days of 1-min bar data
# (price_bars + option_level_bars). Alerts/signals are never pruned.
BAR_RETENTION_DAYS           = int(os.getenv('BAR_RETENTION_DAYS', '10'))
LEVEL_PROXIMITY_PCT          = float(os.getenv('LEVEL_PROXIMITY_PCT', '0.002'))

# ── Data / polling ────────────────────────────────────────────────────────────
BAR_INTERVAL          = 'm1'   # 1-minute bars
POLL_INTERVAL_SECONDS = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))
# Staleness guard: skip a symbol if its newest 1-min bar is older than this many
# seconds (or not today's date). Prevents acting on stale/previous-session data.
MAX_BAR_AGE_SECONDS   = int(os.getenv('MAX_BAR_AGE_SECONDS', '300'))
# Single-instance lock file — prevents two copies running (which doubles alerts).
# Resolved to an absolute path next to this module so launches from different
# working directories contend for the SAME lock (a relative path would let two
# launchers — e.g. the Startup-folder copy and the Task Scheduler watchdog — lock
# different files and both run, doubling every alert).
LOCK_FILE             = os.getenv(
    'LOCK_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jakevolume.lock'),
)
# Minimum seconds between Google Sheets writes (quota is ~60 writes/min/user).
SHEETS_MIN_WRITE_INTERVAL = float(os.getenv('SHEETS_MIN_WRITE_INTERVAL', '1.1'))

# ── Charles Schwab / TD Ameritrade ───────────────────────────────────────────
# Register a developer app at developer.schwab.com to obtain these values.
# Set SCHWAB_CALLBACK_URL to https://127.0.0.1 in your app registration.
SCHWAB_API_KEY      = os.getenv('SCHWAB_API_KEY', '')
SCHWAB_APP_SECRET   = os.getenv('SCHWAB_APP_SECRET', '')
SCHWAB_CALLBACK_URL = os.getenv('SCHWAB_CALLBACK_URL', 'https://127.0.0.1')
SCHWAB_TOKEN_FILE   = os.getenv('SCHWAB_TOKEN_FILE', 'schwab_token.json')

# ── PostgreSQL ────────────────────────────────────────────────────────────────
DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = int(os.getenv('DB_PORT', '5432'))
DB_NAME     = os.getenv('DB_NAME', 'jakevolume')
DB_USER     = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

# ── Volume clustering monitor ─────────────────────────────────────────────────
# Same-day mover: a single 1-min bar with this many contracts triggers FORMING
CLUSTER_VOL_0DTE = int(os.getenv('CLUSTER_VOL_0DTE', '50'))
# Next-expiry positioning: rolling N-bar total must reach this to trigger FORMING
CLUSTER_VOL_NEXT = int(os.getenv('CLUSTER_VOL_NEXT', '150'))
# Rolling window length (bars) used for next-expiry accumulation check
CLUSTER_WINDOW   = int(os.getenv('CLUSTER_WINDOW', '5'))
# Consecutive above-threshold bars required to advance to CONFIRMED
CLUSTER_CONFIRM  = int(os.getenv('CLUSTER_CONFIRM', '3'))
# Consecutive below-threshold bars before an active cluster is marked FADED
CLUSTER_FADE     = int(os.getenv('CLUSTER_FADE', '3'))

# ── Discord ───────────────────────────────────────────────────────────────────
# Set DISCORD_WEBHOOK_URL for signal alerts.
# Set DISCORD_MORNING_WEBHOOK_URL for the 8:20 AM briefing (falls back to DISCORD_WEBHOOK_URL).
DISCORD_WEBHOOK_URL         = os.getenv('DISCORD_WEBHOOK_URL', '')
DISCORD_MORNING_WEBHOOK_URL = os.getenv('DISCORD_MORNING_WEBHOOK_URL', '')

# WATCH alerts are early heads-ups (never traded). They are always recorded in
# Postgres and Google Sheets, but by default are NOT sent to Discord — otherwise
# each setup produces two Discord messages (the WATCH then the real entry) for
# the same ticker/direction, which reads as a duplicate. Set true to also push
# WATCH alerts to Discord.
DISCORD_NOTIFY_WATCH = os.getenv('DISCORD_NOTIFY_WATCH', 'false').lower() == 'true'

# Set SAMPLE_MODE=true in .env to prefix all Discord messages with [SAMPLE].
# Remove or set to false when going live.
SAMPLE_MODE = os.getenv('SAMPLE_MODE', 'false').lower() == 'true'

# ── Alpaca ────────────────────────────────────────────────────────────────────
# Register at alpaca.markets → API Keys to obtain these values.
# Set ALPACA_PAPER=false only when ready to trade with real money.
# Set ALPACA_ENABLED=true to activate auto-execution (default off).
ALPACA_API_KEY      = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY   = os.getenv('ALPACA_SECRET_KEY', '')
ALPACA_PAPER        = os.getenv('ALPACA_PAPER',   'true').lower()  == 'true'
ALPACA_ENABLED      = os.getenv('ALPACA_ENABLED', 'false').lower() == 'true'
TRADE_PCT           = float(os.getenv('TRADE_PCT', '0.01'))   # 1 % of portfolio value per trade
MAX_OPEN_POSITIONS  = int(os.getenv('MAX_OPEN_POSITIONS', '8'))
FLIP_ENABLED        = os.getenv('FLIP_ENABLED', 'false').lower() == 'true'

# ── Google Sheets ─────────────────────────────────────────────────────────────
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'service_account.json')
GOOGLE_SPREADSHEET_ID       = os.getenv('GOOGLE_SPREADSHEET_ID', '')
GOOGLE_FOLDER_ID            = os.getenv('GOOGLE_FOLDER_ID', '')   # optional; for future sheet-in-folder creation

SHEET_NAMES = {
    'daily_levels':      'Daily_Levels',
    'signals':           'Signals',
    'oi_snapshot':       'OI_Snapshot',
    'morning_sentiment': 'Morning_Sentiment',
    'levels_comparison': 'Levels_Comparison',
    'paper_trades':      'Paper_Trades',
    'review':            'Signal_Review',
}

# Set DISCORD_REVIEW_WEBHOOK_URL for the daily post-close review (falls back to the
# morning briefing webhook, then the main signal webhook).
DISCORD_REVIEW_WEBHOOK_URL = os.getenv('DISCORD_REVIEW_WEBHOOK_URL', '')

# ── Claude Nightly Pipeline (§81-§83) ─────────────────────────────────────────
# Anthropic API key — get one at console.anthropic.com.
# Leave blank to disable the nightly research pipeline entirely.
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
# Model used for the nightly research analysis. Haiku is fast and cheap; upgrade
# to claude-sonnet-4-6 for deeper analysis on high-signal days.
NIGHTLY_PIPELINE_MODEL = os.getenv('NIGHTLY_PIPELINE_MODEL', 'claude-haiku-4-5-20251001')
# Webhook for Claude's nightly research notes (falls back to review → morning → main).
DISCORD_RESEARCH_WEBHOOK_URL = os.getenv('DISCORD_RESEARCH_WEBHOOK_URL', '')

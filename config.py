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

# ── Session timezone ──────────────────────────────────────────────────────────
SESSION_TZ = 'America/Chicago'

# ── Market schedule (all times in CST/CDT) ───────────────────────────────────
SNAPSHOT_HOUR   = 8    # 08:20 CST — pre-market OI snapshot
SNAPSHOT_MINUTE = 20
MARKET_OPEN_HOUR    = 8    # 08:30 CST = 09:30 ET
MARKET_OPEN_MINUTE  = 30
MARKET_CLOSE_HOUR   = 15   # 15:00 CST = 16:00 ET
MARKET_CLOSE_MINUTE = 0

# ── OI level computation ──────────────────────────────────────────────────────
# Strikes considered "near ATM": ± ATM_RANGE_PCT of current price
ATM_RANGE_PCT  = float(os.getenv('ATM_RANGE_PCT', '0.05'))   # 5 %
TOP_N_LEVELS   = int(os.getenv('TOP_N_LEVELS', '3'))          # 3 S + 3 R

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

# ClusterStrength minimum thresholds by level rank (enforced gate, not informational)
CS_THRESHOLD_RANK1 = float(os.getenv('CS_THRESHOLD_RANK1', '0.80'))  # S1/R1
CS_THRESHOLD_RANK2 = float(os.getenv('CS_THRESHOLD_RANK2', '0.70'))  # S2/R2
CS_THRESHOLD_RANK3 = float(os.getenv('CS_THRESHOLD_RANK3', '0.65'))  # S3/R3

# Step 9: Spread filter — block if (ask-bid)/mid > 50%
MAX_SPREAD_PCT = float(os.getenv('MAX_SPREAD_PCT', '0.50'))

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
LOCK_FILE             = os.getenv('LOCK_FILE', 'jakevolume.lock')
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
MAX_OPEN_POSITIONS  = int(os.getenv('MAX_OPEN_POSITIONS', '3'))
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
}

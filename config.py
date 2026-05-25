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
    'MU', 'AMD',
]

MAG7: list[str] = ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA', 'TSLA']

# ── Session timezone ──────────────────────────────────────────────────────────
SESSION_TZ = 'America/Chicago'

# ── Market schedule (all times in CST/CDT) ───────────────────────────────────
SNAPSHOT_HOUR   = 8    # 08:10 CST — pre-market OI snapshot
SNAPSHOT_MINUTE = 10
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

# Step 3: 1-min option volume spike parameters
OPT_SPIKE_LOOKBACK      = int(os.getenv('OPT_SPIKE_LOOKBACK',   '10'))   # N bars for baseline
OPT_MIN_BASELINE_VOL    = int(os.getenv('OPT_MIN_BASELINE_VOL', '10'))   # skip if avg < this
OPT_MIN_SPIKE_RATIO     = float(os.getenv('OPT_MIN_SPIKE_RATIO', '3.0')) # spike = 3× baseline
OPT_EXTREME_SPIKE_RATIO = float(os.getenv('OPT_EXTREME_SPIKE_RATIO', '6.0'))  # Step 7 override

# Per-symbol minimum 1-min spike volume (contracts/bar); TSLA/NVDA are higher-volume
OPT_MIN_SPIKE_VOL: dict[str, int] = {
    'AAPL': 100, 'MSFT': 100, 'AMZN': 100, 'GOOGL': 100, 'META': 100,
    'TSLA': 250, 'NVDA': 250, 'MU': 100, 'AMD': 100,
}
OPT_MIN_SPIKE_VOL_DEFAULT = int(os.getenv('OPT_MIN_SPIKE_VOL_DEFAULT', '100'))

# Step 4: Per-symbol minimum 3-min cluster volume (contracts/3-bar window)
OPT_MIN_CLUSTER_VOL: dict[str, int] = {
    'AAPL': 150, 'MSFT': 150, 'AMZN': 150, 'GOOGL': 150, 'META': 150,
    'TSLA': 300, 'NVDA': 300, 'MU': 150, 'AMD': 150,
}
OPT_MIN_CLUSTER_VOL_DEFAULT = int(os.getenv('OPT_MIN_CLUSTER_VOL_DEFAULT', '150'))

# Step 6: ClusterStrength thresholds (0.45×ATM + 0.35×ITM + 0.20×Timing)
CLUSTER_VALID_THRESHOLD  = float(os.getenv('CLUSTER_VALID_THRESHOLD',  '0.65'))
CLUSTER_STRONG_THRESHOLD = float(os.getenv('CLUSTER_STRONG_THRESHOLD', '0.80'))

# Step 8: Contract low filter — block if mark has run > 2.5× intraday low
CONTRACT_LOW_MAX_DIST = float(os.getenv('CONTRACT_LOW_MAX_DIST', '2.50'))

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
LEVEL_PROXIMITY_PCT          = float(os.getenv('LEVEL_PROXIMITY_PCT', '0.002'))

# ── Data / polling ────────────────────────────────────────────────────────────
BAR_INTERVAL          = 'm1'   # 1-minute bars
POLL_INTERVAL_SECONDS = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))

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
# Set DISCORD_MORNING_WEBHOOK_URL for the 8:10 AM briefing (falls back to DISCORD_WEBHOOK_URL).
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
}

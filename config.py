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
SNAPSHOT_HOUR   = 8    # 08:00 CST — pre-market OI snapshot
SNAPSHOT_MINUTE = 0
MARKET_OPEN_HOUR    = 8    # 08:30 CST = 09:30 ET
MARKET_OPEN_MINUTE  = 30
MARKET_CLOSE_HOUR   = 15   # 15:00 CST = 16:00 ET
MARKET_CLOSE_MINUTE = 0

# ── OI level computation ──────────────────────────────────────────────────────
# Strikes considered "near ATM": ± ATM_RANGE_PCT of current price
ATM_RANGE_PCT  = float(os.getenv('ATM_RANGE_PCT', '0.05'))   # 5 %
TOP_N_LEVELS   = int(os.getenv('TOP_N_LEVELS', '2'))          # 2 S + 2 R

# ── Signal detection ──────────────────────────────────────────────────────────
# Price is "near a level" when within this fraction of the strike
LEVEL_PROXIMITY_PCT          = float(os.getenv('LEVEL_PROXIMITY_PCT', '0.002'))  # 0.2 %
# A bar counts as a "spike" when its volume exceeds this multiple of the rolling avg
VOLUME_SPIKE_MULTIPLIER      = float(os.getenv('VOLUME_SPIKE_MULTIPLIER', '1.5'))
# Number of prior bars used to compute the rolling volume baseline
VOLUME_LOOKBACK_BARS         = int(os.getenv('VOLUME_LOOKBACK_BARS', '20'))
# Consecutive spike bars required to fire a signal
CONSECUTIVE_SPIKES_REQUIRED  = int(os.getenv('CONSECUTIVE_SPIKES_REQUIRED', '3'))
# Minimum minutes between signals on the same level (dedup / cooldown)
SIGNAL_COOLDOWN_MINUTES      = int(os.getenv('SIGNAL_COOLDOWN_MINUTES', '30'))
# Minimum option contracts traded in a 1-minute window to count as a volume cluster
OPT_VOL_MIN_CLUSTER          = int(os.getenv('OPT_VOL_MIN_CLUSTER', '25'))

# ── Data / polling ────────────────────────────────────────────────────────────
BAR_INTERVAL         = 'm1'   # 1-minute bars (webull interval code)
BARS_TO_FETCH        = int(os.getenv('BARS_TO_FETCH', '40'))   # fetch buffer
POLL_INTERVAL_SECONDS = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))

# ── Webull credentials ────────────────────────────────────────────────────────
WEBULL_USERNAME    = os.getenv('WEBULL_USERNAME', '')
WEBULL_PASSWORD    = os.getenv('WEBULL_PASSWORD', '')
WEBULL_TRADE_PIN   = os.getenv('WEBULL_TRADE_PIN', '')
WEBULL_DEVICE_NAME = os.getenv('WEBULL_DEVICE_NAME', 'jakevolume_trader')

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

# ── Google Sheets ─────────────────────────────────────────────────────────────
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'service_account.json')
GOOGLE_SPREADSHEET_ID       = os.getenv('GOOGLE_SPREADSHEET_ID', '')
GOOGLE_FOLDER_ID            = os.getenv('GOOGLE_FOLDER_ID', '')   # optional; for future sheet-in-folder creation

SHEET_NAMES = {
    'daily_levels':      'Daily_Levels',
    'signals':           'Signals',
    'oi_snapshot':       'OI_Snapshot',
    'morning_sentiment': 'Morning_Sentiment',
}

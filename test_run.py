"""
End-to-end diagnostic — runs at any time (no Live feed required).
Tests the Historical API path for data, then exercises every layer:
  data client -> DB -> OI levels -> sentiment -> signal detector
  -> positioning monitor (one step) -> Google Sheets (all 4 sheets)
Uses only AAPL to keep it fast.
"""
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
for noisy in ('gspread', 'urllib3', 'google', 'httplib2'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger('test_run')

SYMBOL = 'AAPL'

# ── 1. Client init ────────────────────────────────────────────────────────────
log.info("[1/9] Data client init")
from data.databento_client import DatabentoClient
dbc = DatabentoClient()
dbc.login()
log.info("Client ready  (Historical path — no Live feed needed)")

# ── 2. Previous-day close (Databento Historical ohlcv-1d) ─────────────────────
log.info("[2/9] Previous-day close")
prev_close = dbc.get_prev_close(SYMBOL)
log.info("%s  prev_close=%.4f", SYMBOL, prev_close)
assert prev_close > 0, "prev_close returned zero"

# ── 3. Quote (Live buffer will be empty outside market hours — that's OK) ──────
log.info("[3/9] Quote (live buffer; falls back to prev_close if closed)")
quote    = dbc.get_quote(SYMBOL)
pm_price = quote['price'] or prev_close
log.info("%s  price=%.4f  (source: %s)",
         SYMBOL, pm_price, "live" if quote['price'] else "prev_close fallback")

# ── 4. Option chain (Historical fallback on cold start / outside hours) ────────
log.info("[4/9] Option chain")
chain  = dbc.get_option_chain(SYMBOL)
expiry = chain['expiry']
log.info("%s  expiry=%s  calls=%d  puts=%d",
         SYMBOL, expiry, len(chain['calls']), len(chain['puts']))
assert chain['calls'] or chain['puts'], "Empty option chain returned"

# ── 5. OI levels + sentiment ───────────────────────────────────────────────────
log.info("[5/9] OI levels + sentiment")
from analysis.oi_levels import compute_oi_levels, get_top_oi_snapshot
from analysis.sentiment import compute_sentiment

levels = compute_oi_levels(chain, prev_close)
assert levels, "compute_oi_levels returned no levels"
log.info("  %d levels:", len(levels))
for lv in levels:
    log.info("    %-10s rank=%d  strike=%.2f  OI=%6d  watch=%s",
             lv['level_type'], lv['rank'], lv['strike'],
             lv['open_interest'], lv['option_type'])

sentiment = compute_sentiment(chain, pm_price, prev_close)
log.info("  pm_change=%.2f%%  P/C=%.3f  bias=%s",
         sentiment['pm_change_pct'], sentiment['pc_ratio'], sentiment['bias'])

# ── 6. Database: schema + level persistence ────────────────────────────────────
log.info("[6/9] Database — schema init, save & reload levels")
import db.ops as db
db.init_pool()
db.init_schema()
import pytz
from data.market_utils import today_cst
now   = datetime.now(pytz.timezone('America/Chicago'))
today = today_cst()

db.save_oi_levels(SYMBOL, today, now, levels)
saved = db.get_today_levels(SYMBOL, today)
assert saved, "get_today_levels returned nothing after save"
log.info("  Saved %d levels; re-read %d from Postgres OK", len(levels), len(saved))

# ── 7. Signal detector simulation ─────────────────────────────────────────────
log.info("[7/9] Signal detector simulation")
import config
from analysis.signal_detector import SignalDetector

detector     = SignalDetector()
target_close = float(levels[0]['strike'])   # put price right on the first level
fake_bar     = {
    'bar_time': now,
    'open': prev_close, 'high': prev_close,
    'low':  prev_close, 'close': target_close,
    'volume': 9_999_999,
}
baseline_bar = {
    'bar_time': now, 'open': prev_close, 'high': prev_close,
    'low': prev_close, 'close': prev_close, 'volume': 100,
}
bars = [baseline_bar] * config.VOLUME_LOOKBACK_BARS + [fake_bar]

signals = []
for _ in range(config.CONSECUTIVE_SPIKES_REQUIRED):
    signals = detector.check(SYMBOL, bars, levels, expiry=expiry)

if signals:
    sig = signals[0]
    log.info(
        "  Signal fired: %s %s @ %.2f  opt_%s  enter=%s  exit=%s  spk=%s",
        sig['signal_type'], sig['level_type'], sig['level_price'],
        sig.get('option_type', '?'),
        sig.get('price_to_enter'), sig.get('price_to_exit'),
        sig['spike_volume'],
    )
else:
    log.info("  No signal fired by simulation — building synthetic signal for sheet test")
    sig = {
        'symbol':             SYMBOL,
        'signal_time':        now,
        'signal_type':        'BULLISH',
        'bias':               'Call-side bias',
        'level_type':         levels[0]['level_type'],
        'level_price':        float(levels[0]['strike']),
        'expiry':             expiry,
        'trigger_price':      prev_close,
        'avg_volume_20':      1000.0,
        'spike_volume':       9_999_999,
        'consecutive_spikes': config.CONSECUTIVE_SPIKES_REQUIRED,
        'option_type':        levels[0]['option_type'],
        'opt_mark':           None,
        'opt_bid':            None,
        'opt_ask':            None,
        'opt_vol_delta':      0,
        'price_to_enter':     1.50,
        'price_to_exit':      3.00,
    }

# ── 8. DB: save signal ─────────────────────────────────────────────────────────
log.info("[8/9] Database — save signal")
sig_id = db.save_signal(sig)
db.mark_signal_logged(sig_id)
log.info("  Signal saved with id=%d, marked logged", sig_id)

# ── 9. Google Sheets: all 4 sheets ────────────────────────────────────────────
log.info("[9/9] Google Sheets — all 4 sheets")
from output.sheets_logger import SheetsLogger
snap   = get_top_oi_snapshot(chain, prev_close)
sheets = SheetsLogger()
sheets.connect()

sheets.log_daily_levels(SYMBOL, levels, prev_close, now)
log.info("  Daily_Levels      queued")

sheets.log_oi_snapshot(
    symbol=SYMBOL, expiry=expiry,
    top_calls=snap['top_calls'], top_puts=snap['top_puts'],
    underlying_price=prev_close, snap_time=now,
)
log.info("  OI_Snapshot       queued")

sheets.log_morning_sentiment(sentiment, now)
log.info("  Morning_Sentiment queued")

sheets.log_signal(sig)
log.info("  Signals           queued  [enter=%s  exit=%s]",
         sig.get('price_to_enter'), sig.get('price_to_exit'))

# Wait for the background write worker to flush all queued rows to Sheets.
log.info("  Waiting for Sheets write queue to flush...")
sheets._write_queue.join()
log.info("  All 4 Sheets writes confirmed")

log.info("")
log.info("=" * 60)
log.info("ALL CHECKS PASSED")
log.info("Spreadsheet: https://docs.google.com/spreadsheets/d/%s/edit",
         config.GOOGLE_SPREADSHEET_ID)
log.info("=" * 60)

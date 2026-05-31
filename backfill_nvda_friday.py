"""
Backfill the full 2026-05-29 (Friday) NVDA 1-minute session from Schwab,
persist it to price_bars, and report the true average volume per minute.

One-off analysis script — safe to re-run (save_bars is ON CONFLICT DO NOTHING).
"""
import logging
import sys
from datetime import datetime
from statistics import mean

import pytz

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)-22s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
for noisy in ('urllib3', 'schwab', 'authlib', 'httpx'):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger('backfill')

import schwab
import schwab.client

import config
import db.ops as db
from data.market_utils import CST
from data.schwab_client import SchwabClient

SYMBOL = 'NVDA'
CST_TZ = pytz.timezone('America/Chicago')

# Full regular session in CST: 08:30–15:00 (covers the close at 15:00)
START = CST_TZ.localize(datetime(2026, 5, 29, 8, 30))
END   = CST_TZ.localize(datetime(2026, 5, 29, 15, 1))

# ── Init ──────────────────────────────────────────────────────────────────────
db.init_pool()
db.init_schema()

sc = SchwabClient()
sc.login()

# ── Fetch full-day 1-min history via raw Schwab price-history endpoint ─────────
PH = schwab.client.Client.PriceHistory
resp = sc._client.get_price_history(
    SYMBOL,
    period_type=PH.PeriodType.DAY,
    frequency_type=PH.FrequencyType.MINUTE,
    frequency=PH.Frequency.EVERY_MINUTE,
    start_datetime=START,
    end_datetime=END,
    need_extended_hours_data=False,
)
resp.raise_for_status()
candles = resp.json().get('candles', [])

bars = []
for c in candles:
    bar_time = datetime.fromtimestamp(c['datetime'] / 1000, tz=pytz.UTC).astimezone(CST)
    # Keep only bars that fall on the Friday CST session
    if bar_time.astimezone(CST_TZ).date() != START.date():
        continue
    bars.append({
        'bar_time': bar_time,
        'open':  float(c['open']),
        'high':  float(c['high']),
        'low':   float(c['low']),
        'close': float(c['close']),
        'volume': int(c['volume']),
    })

bars.sort(key=lambda b: b['bar_time'])

if not bars:
    log.warning("No %s bars returned for %s — Schwab has no minute history for that date.",
                SYMBOL, START.date())
    sys.exit(0)

# ── Persist ───────────────────────────────────────────────────────────────────
db.save_bars(SYMBOL, bars)

# ── Report ────────────────────────────────────────────────────────────────────
vols      = [b['volume'] for b in bars]
avg_vol   = mean(vols)
total_vol = sum(vols)
first     = bars[0]['bar_time'].astimezone(CST_TZ)
last      = bars[-1]['bar_time'].astimezone(CST_TZ)

print()
print("=" * 60)
print(f"  {SYMBOL} — {START.date()} (Friday) 1-min session")
print("=" * 60)
print(f"  Bars (minutes)   : {len(bars)}")
print(f"  Session window   : {first:%H:%M} – {last:%H:%M} CST")
print(f"  Avg volume / min : {avg_vol:,.0f} shares")
print(f"  Min / Max / min  : {min(vols):,} / {max(vols):,}")
print(f"  Total volume     : {total_vol:,}")
print("=" * 60)

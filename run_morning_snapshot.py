"""
Run the morning snapshot immediately for all symbols.
Writes to Postgres + all Google Sheets tabs, then prints a full console briefing.
"""
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)-25s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
for noisy in ('gspread', 'urllib3', 'google', 'httplib2'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger('morning_snapshot')

import config
import db.ops as db
from analysis.oi_levels import compute_oi_levels, get_top_oi_snapshot
from analysis.sentiment import compute_sentiment
from data.market_utils import now_cst, today_cst
from data.schwab_client import SchwabClient
from output.sheets_logger import SheetsLogger
from output.discord_notifier import send_morning_briefing as discord_briefing

# ── Init ──────────────────────────────────────────────────────────────────────
db.init_pool()
db.init_schema()

schwab = SchwabClient()
schwab.login()

sheets = SheetsLogger()
sheets.connect()

now   = now_cst()
today = today_cst()

# ── Per-symbol snapshot ───────────────────────────────────────────────────────
results = []

for symbol in config.SYMBOLS:
    try:
        log.info("Processing %s ...", symbol)
        prev_close = schwab.get_prev_close(symbol)
        quote      = schwab.get_quote(symbol)
        pm_price   = quote['price'] or prev_close

        chain      = schwab.get_option_chain(symbol)
        expiry     = chain['expiry']
        levels     = compute_oi_levels(chain, prev_close)
        snap       = get_top_oi_snapshot(chain, prev_close)
        sentiment  = compute_sentiment(chain, pm_price, prev_close)

        # ── Postgres ──
        db.save_option_chain(
            symbol=symbol, snap_date=today, snap_time=now,
            expiry_date=expiry, contracts=chain['all'],
            underlying_price=prev_close,
        )
        db.save_oi_levels(symbol, today, now, levels)
        db.save_morning_sentiment(symbol, today, sentiment['pc_ratio'], sentiment['bias'], now)

        # ── Google Sheets ──
        sheets.log_daily_levels(symbol, levels, prev_close, now)
        sheets.log_oi_snapshot(
            symbol=symbol, expiry=expiry,
            top_calls=snap['top_calls'], top_puts=snap['top_puts'],
            underlying_price=prev_close, snap_time=now,
        )
        sheets.log_morning_sentiment(sentiment, now)
        sheets.log_comparison_row(
            symbol=symbol, expiry=expiry,
            underlying_price=prev_close, levels=levels,
            snap=snap, computed_at=now,
        )

        supports    = [lv for lv in levels if lv['level_type'] == 'SUPPORT']
        resistances = [lv for lv in levels if lv['level_type'] == 'RESISTANCE']

        results.append({
            'symbol':      symbol,
            'prev_close':  prev_close,
            'pm_price':    pm_price,
            'expiry':      expiry,
            'supports':    supports,
            'resistances': resistances,
            'sentiment':   sentiment,
        })

    except Exception:
        log.exception("Failed for %s", symbol)

# ── Console briefing — all symbols ───────────────────────────────────────────
W = 96
title = f"  JAKEVOLUME MORNING BRIEFING  {now.strftime('%Y-%m-%d %H:%M CST')}  "
print()
print("=" * W)
print(title.center(W, "="))
print("=" * W)

hdr = (f"  {'SYM':<6}  {'Prev':>8}  {'PM':>8}  {'Chg%':>6}  {'Bias':<8}"
       f"  {'P/C':>5}  {'S1':>7}  {'S2':>7}  {'S3':>7}  {'R1':>7}  {'R2':>7}  {'R3':>7}  Expiry")
print(hdr)
print("  " + "-" * (W - 2))

for r in results:
    s   = r['sentiment']
    sup = r['supports']
    res = r['resistances']
    sign = "+" if s['pm_change_pct'] >= 0 else ""

    s1 = f"{sup[0]['strike']:.1f}" if len(sup) > 0 else "  -  "
    s2 = f"{sup[1]['strike']:.1f}" if len(sup) > 1 else "  -  "
    s3 = f"{sup[2]['strike']:.1f}" if len(sup) > 2 else "  -  "
    r1 = f"{res[0]['strike']:.1f}" if len(res) > 0 else "  -  "
    r2 = f"{res[1]['strike']:.1f}" if len(res) > 1 else "  -  "
    r3 = f"{res[2]['strike']:.1f}" if len(res) > 2 else "  -  "

    print(
        f"  {r['symbol']:<6}  "
        f"{r['prev_close']:>8.2f}  "
        f"{r['pm_price']:>8.2f}  "
        f"{sign}{s['pm_change_pct']:>5.2f}%  "
        f"{s['bias']:<8}  "
        f"{s['pc_ratio']:>5.3f}  "
        f"{s1:>7}  {s2:>7}  {s3:>7}  {r1:>7}  {r2:>7}  {r3:>7}  "
        f"{r['expiry']}"
    )

print("=" * W)
print(f"  Sheets written for all {len(results)}/{len(config.SYMBOLS)} symbols.")
print("=" * W)
print()

discord_briefing(results, now)

"""
Run the morning snapshot immediately for all symbols.
Writes to Postgres + all Google Sheets tabs, then prints a full console briefing.

Guarded by `if __name__ == "__main__"` so that merely importing this module (e.g.
for a compile/smoke check) does NOT trigger a live snapshot — the body logs into
Schwab and writes to Postgres + Sheets, which must only happen on explicit run.
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


def _prox_cells(levels, reverse):
    """3 strike cells ordered nearest→furthest; '*' marks the highest-OI strike."""
    ordered = sorted(levels, key=lambda lv: lv['strike'], reverse=reverse)[:3]
    max_oi  = max((lv.get('open_interest', 0) or 0 for lv in ordered), default=0)
    cells   = []
    for i in range(3):
        if i < len(ordered):
            oi   = ordered[i].get('open_interest', 0) or 0
            star = "*" if oi and oi == max_oi else ""
            cells.append(f"{ordered[i]['strike']:.1f}{star}")
        else:
            cells.append("  -  ")
    return cells


def main() -> None:
    # ── Init ──────────────────────────────────────────────────────────────────
    db.init_pool()
    db.init_schema()

    schwab = SchwabClient()
    schwab.login()

    sheets = SheetsLogger()
    sheets.connect()

    now   = now_cst()
    today = today_cst()

    # ── Per-symbol snapshot ────────────────────────────────────────────────────
    results = []
    weekend_gaps = []   # per-symbol weekend/holiday OI gaps → briefing

    for symbol in config.SYMBOLS:
        try:
            log.info("Processing %s ...", symbol)
            prev_close = schwab.get_prev_close(symbol)
            quote      = schwab.get_quote(symbol)
            pm_price   = quote['price'] or prev_close

            chain      = schwab.get_option_chain(symbol)
            expiry     = chain['expiry']
            levels     = compute_oi_levels(chain, pm_price)
            snap       = get_top_oi_snapshot(chain, pm_price)
            sentiment  = compute_sentiment(chain, pm_price, prev_close)

            # ── Postgres ──
            db.save_option_chain(
                symbol=symbol, snap_date=today, snap_time=now,
                expiry_date=expiry, contracts=chain['all'],
                underlying_price=pm_price,
            )
            db.save_oi_levels(symbol, today, now, levels)
            db.save_morning_sentiment(symbol, today, sentiment['pc_ratio'], sentiment['bias'], now)

            # ── Weekend / post-holiday OI gaps (near-dated, multi-expiry) ──
            if config.WEEKEND_OI_GAPS_ENABLED:
                try:
                    near_chains = schwab.get_near_dated_chains(symbol)
                    db.save_near_oi_snapshots(symbol, today, now, near_chains)
                    wk = db.get_weekend_oi_gaps(
                        symbol, today,
                        config.WEEKEND_GAP_MIN_CONTRACTS,
                        config.WEEKEND_GAP_MIN_PCT,
                        config.WEEKEND_GAP_TOP_N,
                    )
                    if wk['gaps'] and (wk.get('gap_days') or 0) >= 2:
                        weekend_gaps.append({'symbol': symbol, **wk})
                except Exception:
                    log.warning("%s: weekend OI gap detection failed", symbol, exc_info=True)

            # ── Google Sheets ──
            sheets.log_daily_levels(symbol, levels, pm_price, now)
            sheets.log_oi_snapshot(
                symbol=symbol, expiry=expiry,
                top_calls=snap['top_calls'], top_puts=snap['top_puts'],
                underlying_price=pm_price, snap_time=now,
            )
            sheets.log_morning_sentiment(sentiment, now)
            sheets.log_comparison_row(
                symbol=symbol, expiry=expiry,
                underlying_price=pm_price, levels=levels,
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

    # ── Console briefing — all symbols ─────────────────────────────────────────
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
        sign = "+" if s['pm_change_pct'] >= 0 else ""

        # Supports sit below spot (nearest = highest strike), resistances above
        # (nearest = lowest strike). Rank 1 = nearest; '*' = dominant OI wall.
        s1, s2, s3 = _prox_cells(r['supports'],    reverse=True)
        r1, r2, r3 = _prox_cells(r['resistances'], reverse=False)

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

    discord_briefing(results, now, weekend_gaps=weekend_gaps)


if __name__ == "__main__":
    main()

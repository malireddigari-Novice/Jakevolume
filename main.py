#!/usr/bin/env python3
"""
Jakevolume — 0DTE OI-based intraday alerting system.

Execution model
---------------
A single blocking loop wakes up every POLL_INTERVAL_SECONDS (60 s default).

08:00 CST  → morning_snapshot()   pull option chains, compute OI levels,
                                   persist to Postgres, write to Google Sheets.
08:30–15:00 → intraday_check()    pull 1-min bars, run signal detector,
                                   log any fired signals.

Usage
-----
    python main.py            # normal run (interactive MFA on first login)
    python main.py --login    # force a fresh Webull login then exit
"""
import argparse
import logging
import sys
import time
from datetime import date

import config
import db.ops as db
from analysis.oi_levels import compute_oi_levels, get_top_oi_snapshot
from analysis.positioning_monitor import PositioningMonitor
from analysis.sentiment import compute_sentiment
from analysis.signal_detector import SignalDetector
from data.market_utils import (
    now_cst, today_cst,
    is_market_open, is_snapshot_window,
)
from data.webull_client import WebullClient
from output.sheets_logger import SheetsLogger


# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure root logger to write INFO+ to stdout and a rotating log file."""
    fmt = '%(asctime)s %(levelname)-8s %(name)-30s %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('jakevolume.log', encoding='utf-8'),
        ],
    )
    # Reduce noise from third-party libs
    for noisy in ('gspread', 'urllib3', 'google', 'httplib2', 'webull'):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger('jakevolume.main')


# ── Morning snapshot (08:00 CST, once per trading day) ───────────────────────

def morning_snapshot(wb: WebullClient, sheets: SheetsLogger) -> None:
    """
    Run the daily 08:00 CST setup: pull option chains, compute OI S/R levels,
    persist to Postgres, and enqueue all rows for Google Sheets.  Failures for
    individual symbols are logged but do not abort the remaining symbols.
    """
    now   = now_cst()
    today = today_cst()
    logger.info("═══ MORNING SNAPSHOT START (%s) ═══", now.strftime('%Y-%m-%d %H:%M CST'))

    sentiments: list[dict] = []

    for symbol in config.SYMBOLS:
        try:
            # Explicit previous-day close — used as the ATM anchor for OI levels
            prev_close = wb.get_prev_close(symbol)

            # Pre-market quote from Live buffer; falls back to prev_close if
            # no Live bar has arrived yet (normal before first 1-min bar).
            quote    = wb.get_quote(symbol)
            pm_price = quote['price'] or prev_close

            logger.info("%s: prev_close=%.4f  pm_price=%.4f", symbol, prev_close, pm_price)

            # 0DTE option chain
            chain  = wb.get_option_chain(symbol)
            expiry = chain['expiry']

            # OI levels anchored to prev_close (not pre-market price)
            levels = compute_oi_levels(chain, prev_close)

            # Top-3 OI snapshot
            snap = get_top_oi_snapshot(chain, prev_close)

            # Sentiment (pre-market drift + put/call OI ratio)
            sentiment = compute_sentiment(chain, pm_price, prev_close)
            sentiments.append(sentiment)

            # ── Persist to Postgres ──
            db.save_option_chain(
                symbol=symbol,
                snap_date=today,
                snap_time=now,
                expiry_date=expiry,
                contracts=chain['all'],
                underlying_price=prev_close,
            )
            db.save_oi_levels(symbol, today, now, levels)

            # ── Log to Google Sheets ──
            sheets.log_daily_levels(symbol, levels, prev_close, now)
            sheets.log_oi_snapshot(
                symbol=symbol,
                expiry=expiry,
                top_calls=snap['top_calls'],
                top_puts=snap['top_puts'],
                underlying_price=prev_close,
                snap_time=now,
            )
            sheets.log_morning_sentiment(sentiment, now)

        except Exception:
            logger.exception("Morning snapshot failed for %s", symbol)

    _print_mag7_briefing(sentiments, now)
    logger.info("═══ MORNING SNAPSHOT COMPLETE ═══")


def _print_mag7_briefing(sentiments: list[dict], now) -> None:
    """Print a formatted MAG7 morning briefing to the console."""
    mag7_rows = [s for s in sentiments if s['symbol'] in config.MAG7]
    if not mag7_rows:
        return

    header = f"  {'Symbol':<6}  {'Prev Close':>10}  {'PM Price':>8}  {'Change%':>8}  {'P/C Ratio':>9}  Bias"
    divider = "  " + "─" * (len(header) - 2)
    width   = max(len(header), 62)
    title   = f" MAG7 MORNING SENTIMENT — {now.strftime('%Y-%m-%d %H:%M CST')} "

    print()
    print("═" * width)
    print(title.center(width, "═"))
    print("═" * width)
    print(header)
    print(divider)

    for s in mag7_rows:
        sign = "+" if s['pm_change_pct'] >= 0 else ""
        print(
            f"  {s['symbol']:<6}  "
            f"{s['prev_close']:>10.2f}  "
            f"{s['pm_price']:>8.2f}  "
            f"{sign}{s['pm_change_pct']:>7.2f}%  "
            f"{s['pc_ratio']:>9.3f}  "
            f"{s['bias']}"
        )

    print("═" * width)
    print()


# ── Intraday check (every minute during market hours) ────────────────────────

def intraday_check(
    wb: WebullClient,
    detector: SignalDetector,
    monitor: PositioningMonitor,
    sheets: SheetsLogger,
) -> None:
    """
    Scan all symbols once per 60-second poll: pull bars, run the signal detector,
    fire desktop notifications, and update the positioning monitor.  Failures for
    individual symbols are logged without interrupting the remaining symbols.
    """
    today = today_cst()

    for symbol in config.SYMBOLS:
        try:
            # Pull latest 1-min equity bars
            bars = wb.get_bars(symbol)
            if not bars:
                logger.warning("%s: no bars returned", symbol)
                continue

            db.save_bars(symbol, bars)
            underlying_price = bars[-1]['close']

            # Load today's OI-derived S/R levels
            levels = db.get_today_levels(symbol, today)
            if not levels:
                logger.debug("%s: no OI levels for %s, skipping signal check", symbol, today)
                continue

            # Pull live option quotes for the specific S/R strikes
            option_quotes: dict = {}
            expiry = None
            try:
                expiry = wb.get_nearest_expiry(symbol)
                option_quotes = wb.get_option_quotes_for_levels(symbol, expiry, levels)
            except Exception:
                logger.warning("%s: option quote fetch failed, proceeding without", symbol)

            # Detect volume clusters — equity spike OR option volume cluster at S/R level
            signals = detector.check(symbol, bars, levels, option_quotes, expiry=expiry)

            for sig in signals:
                sig_id = db.save_signal(sig)
                sheets.log_signal(sig)
                db.mark_signal_logged(sig_id)
                _notify_signal(sig)

            # Volume cluster positioning monitor (Postgres only, no signals)
            try:
                expiry_pair = wb.get_expiry_pair(symbol)
                atm_quotes  = wb.get_atm_option_quotes_all_expiries(symbol, underlying_price)
                monitor.update(symbol, atm_quotes, expiry_pair, levels, underlying_price)
            except Exception:
                logger.warning("%s: positioning monitor update failed", symbol, exc_info=True)

        except Exception:
            logger.exception("Intraday check failed for %s", symbol)


# ── Desktop notification ──────────────────────────────────────────────────────

def _notify_signal(sig: dict) -> None:
    """Best-effort desktop notification when a signal fires."""
    try:
        from plyer import notification  # optional dependency
        title = f"Jakevolume: {sig['signal_type']} {sig['symbol']}"
        enter = f"${sig['price_to_enter']:.2f}" if sig.get('price_to_enter') else 'n/a'
        exit_ = f"${sig['price_to_exit']:.2f}"  if sig.get('price_to_exit')  else 'n/a'
        msg = (
            f"{sig.get('option_type','?')}  enter={enter}  exit={exit_}\n"
            f"spike={sig['spike_volume']}  trigger={sig['trigger_price']:.2f}"
        )
        notification.notify(title=title, message=msg, timeout=10)
    except Exception:
        pass


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    """
    Entry point: initialise all subsystems, start Live feeds, then run the
    blocking 60-second poll loop until the process is terminated.
    """
    parser = argparse.ArgumentParser(description='Jakevolume 0DTE alerting system')
    parser.add_argument('--login', action='store_true',
                        help='Validate credentials and exit (no live feed started)')
    args = parser.parse_args()

    _setup_logging()
    logger.info(
        "Jakevolume starting  symbols=%d  poll=%ds  S/R levels=%d+%d",
        len(config.SYMBOLS),
        config.POLL_INTERVAL_SECONDS,
        config.TOP_N_LEVELS,
        config.TOP_N_LEVELS,
    )

    # ── Initialise subsystems ──
    db.init_pool()
    db.init_schema()

    wb = WebullClient()
    wb.login(interactive=True)

    if args.login:
        logger.info("--login flag: session cached. Exiting.")
        return

    wb.start_live_feed()   # open Databento Live sessions (daemon threads)

    sheets = SheetsLogger()
    sheets.connect()

    detector   = SignalDetector()
    monitor    = PositioningMonitor()
    snap_done: date | None = None   # guard: run snapshot only once per day

    logger.info(
        "Loop running. Snapshot @ 08:00 CST | Market hours 08:30–15:00 CST"
    )

    while True:
        t0  = time.monotonic()
        now = now_cst()

        try:
            # Morning snapshot — once per trading day
            if is_snapshot_window(now) and snap_done != now.date():
                morning_snapshot(wb, sheets)
                snap_done = now.date()

            # Intraday signal scan — every minute during market hours
            if is_market_open(now):
                intraday_check(wb, detector, monitor, sheets)

        except Exception:
            logger.exception("Unhandled error in main loop")

        # Sleep for the remainder of the 60-second interval
        elapsed   = time.monotonic() - t0
        sleep_for = max(0.0, config.POLL_INTERVAL_SECONDS - elapsed)
        logger.debug("Sleeping %.1fs until next poll", sleep_for)
        time.sleep(sleep_for)


if __name__ == '__main__':
    main()

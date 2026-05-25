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
from typing import Optional

import config
import db.ops as db
from analysis.oi_levels import compute_oi_levels, get_top_oi_snapshot
from analysis.positioning_monitor import PositioningMonitor
from analysis.sentiment import compute_sentiment
from analysis.signal_detector import SignalDetector
from data.market_utils import (
    now_cst, today_cst,
    is_market_open, is_snapshot_window, is_eod_window,
)
from data.schwab_client import SchwabClient
from data.databento_client import DatabentoClient
from data.alpaca_client import AlpacaClient, occ_symbol
from output.sheets_logger import SheetsLogger
from output.discord_notifier import (
    send_signal as discord_signal,
    send_morning_briefing as discord_briefing,
    send_trade_alert as discord_trade,
    send_exit_alert as discord_exit,
)


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

def morning_snapshot(schwab: SchwabClient, sheets: SheetsLogger) -> None:
    """
    Run the daily 08:10 CST setup using Schwab for option chains, quotes, and prices.
    """
    now   = now_cst()
    today = today_cst()
    logger.info("═══ MORNING SNAPSHOT START (%s) ═══", now.strftime('%Y-%m-%d %H:%M CST'))

    sentiments: list[dict] = []

    for symbol in config.SYMBOLS:
        try:
            prev_close = schwab.get_prev_close(symbol)

            quote    = schwab.get_quote(symbol)
            pm_price = quote['price'] or prev_close

            logger.info("%s: prev_close=%.4f  pm_price=%.4f", symbol, prev_close, pm_price)

            chain  = schwab.get_option_chain(symbol)
            expiry = chain['expiry']

            # OI levels anchored to prev_close (not pre-market price)
            levels = compute_oi_levels(chain, prev_close)

            # Top-3 OI snapshot
            snap = get_top_oi_snapshot(chain, prev_close)

            # Sentiment (pre-market drift + put/call OI ratio)
            sentiment = compute_sentiment(chain, pm_price, prev_close)
            sentiment['levels'] = levels   # carried into _print_mag7_briefing
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
            db.save_morning_sentiment(symbol, today, sentiment['pc_ratio'], sentiment['bias'], now)

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
            sheets.log_comparison_row(
                symbol=symbol,
                expiry=expiry,
                underlying_price=prev_close,
                levels=levels,
                snap=snap,
                computed_at=now,
            )

        except Exception:
            logger.exception("Morning snapshot failed for %s", symbol)

    _print_mag7_briefing(sentiments, now)

    # Send morning briefing to Discord
    discord_results = [
        {
            'symbol':      s['symbol'],
            'prev_close':  s['prev_close'],
            'pm_price':    s['pm_price'],
            'expiry':      next(
                (lv.get('expiry') for lv in s.get('levels', [])), None
            ),
            'supports':    sorted(
                [lv for lv in s.get('levels', []) if lv['level_type'] == 'SUPPORT'],
                key=lambda x: x['rank'],
            ),
            'resistances': sorted(
                [lv for lv in s.get('levels', []) if lv['level_type'] == 'RESISTANCE'],
                key=lambda x: x['rank'],
            ),
            'sentiment':   s,
        }
        for s in sentiments
    ]
    discord_briefing(discord_results, now)

    logger.info("═══ MORNING SNAPSHOT COMPLETE ═══")


def _print_mag7_briefing(sentiments: list[dict], now) -> None:
    """Print a formatted MAG7 morning briefing to the console."""
    mag7_rows = [s for s in sentiments if s['symbol'] in config.MAG7]
    if not mag7_rows:
        return

    header = (
        f"  {'Symbol':<6}  {'Prev':>8}  {'PM':>8}  {'Chg%':>7}  "
        f"{'P/C':>5}  {'S1':>7}  {'S2':>7}  {'S3':>7}  "
        f"{'R1':>7}  {'R2':>7}  {'R3':>7}  Bias"
    )
    divider = "  " + "-" * (len(header) - 2)
    width   = max(len(header), 70)
    title   = f" MAG7 MORNING SENTIMENT -- {now.strftime('%Y-%m-%d %H:%M CST')} "

    print()
    print("=" * width)
    print(title.center(width, "="))
    print("=" * width)
    print(header)
    print(divider)

    for s in mag7_rows:
        sign = "+" if s['pm_change_pct'] >= 0 else ""
        lvls = s.get('levels', [])
        sup  = sorted([l for l in lvls if l['level_type'] == 'SUPPORT'],    key=lambda x: x['rank'])
        res  = sorted([l for l in lvls if l['level_type'] == 'RESISTANCE'], key=lambda x: x['rank'])
        s1 = f"{sup[0]['strike']:.1f}" if len(sup) > 0 else "  - "
        s2 = f"{sup[1]['strike']:.1f}" if len(sup) > 1 else "  - "
        s3 = f"{sup[2]['strike']:.1f}" if len(sup) > 2 else "  - "
        r1 = f"{res[0]['strike']:.1f}" if len(res) > 0 else "  - "
        r2 = f"{res[1]['strike']:.1f}" if len(res) > 1 else "  - "
        r3 = f"{res[2]['strike']:.1f}" if len(res) > 2 else "  - "
        print(
            f"  {s['symbol']:<6}  "
            f"{s['prev_close']:>8.2f}  "
            f"{s['pm_price']:>8.2f}  "
            f"{sign}{s['pm_change_pct']:>6.2f}%  "
            f"{s['pc_ratio']:>5.3f}  "
            f"{s1:>7}  {s2:>7}  {s3:>7}  "
            f"{r1:>7}  {r2:>7}  {r3:>7}  "
            f"{s['bias']}"
        )

    print("=" * width)
    print()


# ── Intraday check (every minute during market hours) ────────────────────────

def intraday_check(
    dbc: DatabentoClient,
    detector: SignalDetector,
    monitor: PositioningMonitor,
    sheets: SheetsLogger,
    schwab: Optional[SchwabClient] = None,
    alpaca: Optional[AlpacaClient] = None,
) -> None:
    """
    Scan all symbols once per 60-second poll: pull bars, run the signal detector,
    fire desktop notifications, and update the positioning monitor.  Failures for
    individual symbols are logged without interrupting the remaining symbols.

    If a SchwabClient is provided its real-time bid/ask prices are merged into
    the option quotes so price_to_enter and price_to_exit are always populated.
    """
    today = today_cst()

    for symbol in config.SYMBOLS:
        try:
            # 1-min equity bars — Schwab primary, Databento Live fallback
            if schwab:
                bars = schwab.get_bars(symbol)
            else:
                bars = dbc.get_bars(symbol)

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

            # Watched contracts — 2 nearest strikes to current spot, per side.
            # Calls watched at support, puts at resistance.
            # Primary = highest OI of the 2 nearest (most liquid, first choice).
            # Secondary = other nearest (used for ATM/ITM cluster checks).
            option_quotes: dict = {}
            expiry = None
            try:
                expiry = schwab.get_nearest_expiry(symbol) if schwab else dbc.get_nearest_expiry(symbol)
                if schwab and expiry:
                    option_quotes = schwab.get_watched_contracts(
                        symbol, expiry, underlying_price, n=2
                    )
                    logger.debug(
                        "%s: watched contracts near %.2f — %d quotes (expiry %s)",
                        symbol, underlying_price, len(option_quotes), expiry,
                    )
                elif expiry:
                    option_quotes = dbc.get_option_quotes_for_levels(
                        symbol, expiry, levels
                    )
            except Exception:
                logger.warning("%s: option quote fetch failed, proceeding without", symbol)

            # Morning P/C ratio for conviction multiplier
            pc_ratio = db.get_today_pc_ratio(symbol, today)

            signals = detector.check(symbol, bars, levels, option_quotes, expiry=expiry, pc_ratio=pc_ratio)

            for sig in signals:
                sig_id = db.save_signal(sig)
                sheets.log_signal(sig)
                db.mark_signal_logged(sig_id)
                _notify_signal(sig)
                if alpaca and config.ALPACA_ENABLED:
                    _execute_trade(sig, sig_id, alpaca)

            # Exit monitoring — check R1/R2 or S1/S2 targets for open trades
            if alpaca and config.ALPACA_ENABLED:
                check_exits(symbol, underlying_price, alpaca, now_cst())

            # Volume cluster positioning monitor (Postgres only, no signals)
            try:
                expiry_pair = dbc.get_expiry_pair(symbol)
                atm_quotes  = dbc.get_atm_option_quotes_all_expiries(symbol, underlying_price)
                monitor.update(symbol, atm_quotes, expiry_pair, levels, underlying_price)
            except Exception:
                logger.warning("%s: positioning monitor update failed", symbol, exc_info=True)

        except Exception:
            logger.exception("Intraday check failed for %s", symbol)


# ── Desktop notification ──────────────────────────────────────────────────────

def _notify_signal(sig: dict) -> None:
    """Best-effort desktop + Discord notification when a signal fires."""
    try:
        discord_signal(sig)
    except Exception:
        pass

    try:
        from plyer import notification
        title = f"Jakevolume: {sig['signal_type']} {sig['symbol']}"
        enter = f"${sig['price_to_enter']:.2f}" if sig.get('price_to_enter') else 'n/a'
        exit_ = f"${sig['price_to_exit']:.2f}"  if sig.get('price_to_exit')  else 'n/a'
        msg = (
            f"{sig.get('option_type','?')}  enter={enter}  exit={exit_}\n"
            f"trigger={sig['trigger_price']:.2f}  room={sig.get('room_score','')}"
        )
        notification.notify(title=title, message=msg, timeout=10)
    except Exception:
        pass


# ── Alpaca trade execution ────────────────────────────────────────────────────

def _execute_trade(sig: dict, sig_id: int, alpaca: AlpacaClient) -> None:
    """
    Place a buy-to-open option order on Alpaca, computing exit targets from today's levels.

    Exit logic:
      BULLISH (call at support) → sell 1/2 at R1, sell 1/2 at R2
      BEARISH (put at resistance) → sell 1/2 at S1, sell 1/2 at S2

    Skipped if price_to_enter is missing, expiry is missing,
    already at MAX_OPEN_POSITIONS, or portfolio too small for 1 contract.
    """
    symbol      = sig.get('symbol', '')
    price       = sig.get('price_to_enter')
    expiry      = sig.get('expiry')
    strike      = sig.get('level_price')
    option_type = sig.get('option_type')
    signal_type = sig.get('signal_type', '')

    if not price:
        logger.warning("Alpaca: trade skipped for %s — no price_to_enter", symbol)
        return
    if not expiry:
        logger.warning("Alpaca: trade skipped for %s — no expiry in signal", symbol)
        return

    open_pos = alpaca.open_position_count()
    if open_pos >= config.MAX_OPEN_POSITIONS:
        logger.info(
            "Alpaca: trade skipped for %s — at MAX_OPEN_POSITIONS (%d/%d)",
            symbol, open_pos, config.MAX_OPEN_POSITIONS,
        )
        return

    qty, spend = alpaca.calculate_qty(price)
    if qty < 1:
        logger.warning(
            "Alpaca: trade skipped for %s — insufficient portfolio value "
            "(%.0f%% = $%.2f / $%.2f per contract < 1 contract)",
            symbol, config.TRADE_PCT * 100, spend, price * 100,
        )
        return

    # ── Determine exit targets from today's OI levels ──
    exit1_underlying = exit2_underlying = None
    try:
        levels = db.get_today_levels(symbol, today_cst())
        if signal_type == 'BULLISH':
            targets = sorted(
                [lv for lv in levels if lv['level_type'] == 'RESISTANCE'],
                key=lambda x: x['rank'],
            )
        else:
            targets = sorted(
                [lv for lv in levels if lv['level_type'] == 'SUPPORT'],
                key=lambda x: x['rank'],
            )
        if len(targets) >= 1:
            exit1_underlying = float(targets[0]['strike'])
        if len(targets) >= 2:
            exit2_underlying = float(targets[1]['strike'])
    except Exception:
        logger.warning("Alpaca: could not resolve exit targets for %s", symbol, exc_info=True)

    exit1_qty = qty // 2
    exit2_qty = qty - exit1_qty   # remaining (handles odd numbers)

    logger.info(
        "Alpaca: %s %s  entry=$%.2f  exit1=%s@%s  exit2=%s@%s",
        signal_type, symbol, price,
        exit1_qty, exit1_underlying, exit2_qty, exit2_underlying,
    )

    order = alpaca.place_option_order(
        symbol=symbol, expiry=expiry, strike=strike,
        option_type=option_type, qty=qty, limit_price=price,
    )

    if order:
        db.save_trade({
            'signal_id':         sig_id,
            'symbol':            symbol,
            'occ_symbol':        order.get('symbol', occ_symbol(symbol, expiry, strike, option_type)),
            'alpaca_order_id':   order.get('id'),
            'qty':               qty,
            'limit_price':       price,
            'buying_power_used': spend,
            'paper':             config.ALPACA_PAPER,
            'status':            'placed',
            'signal_type':       signal_type,
            'exit1_underlying':  exit1_underlying,
            'exit2_underlying':  exit2_underlying,
            'exit1_qty':         exit1_qty,
            'exit2_qty':         exit2_qty,
        })
        try:
            discord_trade(order, sig, qty, spend)
        except Exception:
            pass


def check_exits(
    symbol: str,
    underlying_price: float,
    alpaca: AlpacaClient,
    now: datetime,
) -> None:
    """
    For every open trade on this symbol, check whether the underlying has
    reached the exit targets and fire partial sell orders accordingly.

    BULLISH: exit1 when price >= R1, exit2 when price >= R2 (after exit1 filled)
    BEARISH: exit1 when price <= S1, exit2 when price <= S2 (after exit1 filled)
    """
    trades = db.get_open_trades(symbol)
    for trade in trades:
        sig_type = trade.get('signal_type', '')
        occ      = trade['occ_symbol']

        # ── Exit 1 ──────────────────────────────────────────────────────────
        if not trade['exit1_filled'] and trade.get('exit1_underlying'):
            target = float(trade['exit1_underlying'])
            hit = (
                (sig_type == 'BULLISH' and underlying_price >= target) or
                (sig_type == 'BEARISH' and underlying_price <= target)
            )
            if hit:
                logger.info(
                    "Exit1 triggered  %s  spot=%.2f  target=%.2f  qty=%d",
                    occ, underlying_price, target, trade['exit1_qty'],
                )
                order = alpaca.close_position_qty(occ, trade['exit1_qty'])
                if order:
                    db.mark_exit1_filled(trade['id'], now)
                    label = f"Exit 1/2 @ {'R1' if sig_type=='BULLISH' else 'S1'}"
                    try:
                        discord_exit(order, dict(trade), label, underlying_price)
                    except Exception:
                        pass

        # ── Exit 2 — only after exit1 is confirmed ──────────────────────────
        elif trade['exit1_filled'] and not trade['exit2_filled'] and trade.get('exit2_underlying'):
            target = float(trade['exit2_underlying'])
            hit = (
                (sig_type == 'BULLISH' and underlying_price >= target) or
                (sig_type == 'BEARISH' and underlying_price <= target)
            )
            if hit:
                logger.info(
                    "Exit2 triggered  %s  spot=%.2f  target=%.2f  qty=%d",
                    occ, underlying_price, target, trade['exit2_qty'],
                )
                order = alpaca.close_position_qty(occ, trade['exit2_qty'])
                if order:
                    db.mark_exit2_filled(trade['id'], now)
                    label = f"Exit 2/2 @ {'R2' if sig_type=='BULLISH' else 'S2'}"
                    try:
                        discord_exit(order, dict(trade), label, underlying_price)
                    except Exception:
                        pass


def eod_liquidate(alpaca: AlpacaClient, now: datetime) -> None:
    """
    Close every open option position at market (14:55 CST).
    Marks all remaining open trades as eod_closed in the DB.
    """
    logger.info("EOD liquidation starting at %s", now.strftime('%H:%M CST'))
    count = alpaca.close_all_positions()

    open_trades = db.get_open_trades()
    for trade in open_trades:
        db.mark_trade_eod_closed(trade['id'], now)
        try:
            discord_exit(
                {'id': 'eod', 'qty': trade['exit2_qty'] or trade['exit1_qty']},
                dict(trade),
                'EOD Close',
                0.0,
            )
        except Exception:
            pass

    logger.info("EOD liquidation complete — %d position(s) closed", count)


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

    dbc = DatabentoClient()
    dbc.login(interactive=True)

    if args.login:
        logger.info("--login flag: session cached. Exiting.")
        return

    # Databento Live is used only for the positioning monitor.
    # A missing live license is non-fatal — Schwab handles all intraday signals.
    try:
        dbc.start_live_feed()
    except Exception:
        logger.warning(
            "Databento Live feed unavailable (no live license?) — "
            "positioning monitor will be skipped; Schwab handles all intraday data.",
            exc_info=True,
        )

    sheets = SheetsLogger()
    sheets.connect()

    # Schwab client: provides real-time bid/ask for price_to_enter / price_to_exit.
    # Skipped gracefully if credentials are not configured.
    schwab: Optional[SchwabClient] = None
    if config.SCHWAB_API_KEY and config.SCHWAB_APP_SECRET:
        try:
            schwab = SchwabClient()
            schwab.login()
            logger.info("Schwab client ready — real-time option bid/ask enabled")
        except Exception:
            logger.warning(
                "Schwab login failed — price_to_enter/exit will be None in signals",
                exc_info=True,
            )
            schwab = None
    else:
        logger.warning(
            "SCHWAB_API_KEY / SCHWAB_APP_SECRET not set — "
            "price_to_enter/exit will be None. Add credentials to .env."
        )

    # Alpaca: auto-execute trades when ALPACA_ENABLED=true
    alpaca: Optional[AlpacaClient] = None
    if config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY:
        try:
            alpaca = AlpacaClient()
            if alpaca.verify():
                mode = "PAPER" if config.ALPACA_PAPER else "LIVE"
                if config.ALPACA_ENABLED:
                    logger.info(
                        "Alpaca %s ready — auto-execution ON  "
                        "trade_pct=%.0f%%  max_positions=%d",
                        mode, config.TRADE_PCT * 100, config.MAX_OPEN_POSITIONS,
                    )
                else:
                    logger.info(
                        "Alpaca %s ready — auto-execution OFF "
                        "(set ALPACA_ENABLED=true to activate)", mode,
                    )
            else:
                alpaca = None
        except Exception:
            logger.warning("Alpaca init failed", exc_info=True)
            alpaca = None
    else:
        logger.warning(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set — "
            "auto-execution disabled. Add credentials to .env."
        )

    detector   = SignalDetector()
    monitor    = PositioningMonitor()
    snap_done: date | None = None   # guard: run snapshot only once per day
    eod_done:  date | None = None   # guard: run EOD liquidation only once per day

    logger.info(
        "Loop running. Snapshot @ 08:10 CST | Market hours 08:30–15:00 CST"
    )

    while True:
        t0  = time.monotonic()
        now = now_cst()

        try:
            # Morning snapshot — once per trading day
            if is_snapshot_window(now) and snap_done != now.date():
                if schwab:
                    morning_snapshot(schwab, sheets)
                else:
                    logger.warning("Schwab not available — morning snapshot skipped")
                snap_done = now.date()

            # Intraday signal scan — every minute during market hours
            if is_market_open(now):
                intraday_check(dbc, detector, monitor, sheets, schwab=schwab, alpaca=alpaca)

            # EOD liquidation — 14:55 CST, once per day
            if alpaca and config.ALPACA_ENABLED and is_eod_window(now) and eod_done != now.date():
                eod_liquidate(alpaca, now)
                eod_done = now.date()

        except Exception:
            logger.exception("Unhandled error in main loop")

        # Sleep for the remainder of the 60-second interval
        elapsed   = time.monotonic() - t0
        sleep_for = max(0.0, config.POLL_INTERVAL_SECONDS - elapsed)
        logger.debug("Sleeping %.1fs until next poll", sleep_for)
        time.sleep(sleep_for)


if __name__ == '__main__':
    main()

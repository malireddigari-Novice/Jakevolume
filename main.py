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
import logging.handlers
import sys
import time
from datetime import date, datetime
from typing import Optional

import config
import db.ops as db
import single_instance
from analysis.oi_levels import compute_oi_levels, get_top_oi_snapshot, compute_secondary_watchlist
from analysis.positioning_monitor import PositioningMonitor
from analysis.sentiment import compute_sentiment
from analysis.signal_detector import SignalDetector, compute_exit_targets
from analysis.daily_review import analyze_daily_signals
from analysis.nightly_pipeline import run_nightly_pipeline
from analysis.flow_reversal import FlowReversalEngine, volume_event
from analysis.volume_analytics import compute_leadership_scores
from analysis.open_positions import collect_open_positions
from analysis import gold_mode
from analysis import relative_strength as rs
from analysis import chandelier
from analysis.intent_gate import IntentGate
from analysis.paper_fill import price_moved_from_event
from output.discord_notifier import send_reversal_alert
from data.market_utils import (
    now_cst, today_cst,
    is_market_open, is_snapshot_window, is_past_snapshot, is_eod_window, is_opening_range,
    is_post_close,
)
from data.schwab_client import SchwabClient
from data.databento_client import DatabentoClient
from data.alpaca_client import AlpacaClient, occ_symbol
from data.alpaca_data_client import AlpacaDataClient
from output.sheets_logger import SheetsLogger
from output.discord_notifier import (
    send_signal as discord_signal,
    send_morning_briefing as discord_briefing,
)


# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure root logger to write INFO+ to stdout and a rotating log file."""
    # Make the console handler tolerate non-ASCII (e.g. → arrows) on Windows
    # code pages; without this, logging a "→" raises UnicodeEncodeError. The
    # file handler is already UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    fmt = '%(asctime)s %(levelname)-8s %(name)-30s %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Rotating file log: cap at ~10 MB × 5 backups (~60 MB total) so
            # jakevolume.log never grows unbounded. An already-oversized file
            # rolls over on the first write after restart.
            logging.handlers.RotatingFileHandler(
                'jakevolume.log', maxBytes=10 * 1024 * 1024, backupCount=5,
                encoding='utf-8',
            ),
        ],
    )
    # Reduce noise from third-party libs
    for noisy in ('gspread', 'urllib3', 'google', 'httplib2', 'webull'):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger('jakevolume.main')

# Per-symbol flow-leadership reversal engine (spec §19), shared across polls.
_reversal_engine = FlowReversalEngine()

# Deferred Gold-entry intent gate (P2). Only active when GOLD_ONLY_PRODUCTION_MODE
# AND INTENT_VALIDATION_ENABLED; holds pending candidates awaiting confirmation.
_intent_gate = IntentGate()


# ── Morning snapshot (08:00 CST, once per trading day) ───────────────────────

def _spot_anchor(symbol: str, schwab: SchwabClient, adata=None):
    """(prev_close, spot) for a symbol — Alpaca SIP mid preferred, then Schwab, then
    prev_close. Same anchor logic the MAG7 loop uses; reused for SPY/QQQ benchmarks."""
    prev_close = schwab.get_prev_close(symbol)
    spot = None
    if adata is not None:
        try:
            spot = adata.get_quote_mid(symbol)
        except Exception:
            spot = None
    if not spot:
        try:
            spot = schwab.get_quote(symbol).get('price')
        except Exception:
            spot = None
    if not spot:
        spot = prev_close
    return prev_close, spot


def _compute_morning_relative_strength(sentiments: list, schwab, adata, today, now):
    """
    Pull SPY/QQQ (context only) and compute each MAG7 name's raw relative return vs
    RS_BENCHMARK (QQQ). Annotates each MAG7 sentiment in place with rs/rs_class/rs_tag.
    Returns (benchmarks, divergences) for the briefing; ([], []) when disabled/unavailable.
    """
    if not config.RELATIVE_STRENGTH_ENABLED:
        return [], []
    benchmarks, bench_map = [], {}
    for b in config.BENCHMARKS:
        try:
            bprev, bspot = _spot_anchor(b, schwab, adata)
            bp = rs.pct_change(bspot, bprev)
            bench_map[b] = {'pct': bp, 'spot': bspot}
            benchmarks.append({'symbol': b, 'prev_close': bprev, 'pm_price': bspot, 'pct': bp})
            logger.info("BENCHMARK %s: prev=%.2f spot=%.2f chg=%s%%", b,
                        bprev or 0.0, bspot or 0.0, bp)
        except Exception:
            logger.warning("Benchmark %s pull failed", b, exc_info=True)
    bench = bench_map.get(config.RS_BENCHMARK)
    bench_pct = bench['pct'] if bench else None
    bench_spot = bench['spot'] if bench else None
    if bench_pct is None:
        logger.warning("Relative strength: no %s benchmark %% — skipping RS", config.RS_BENCHMARK)
        return benchmarks, []
    thr = config.RS_DIVERGENCE_PCT
    rows = []
    for s in sentiments:
        if s['symbol'] not in config.MAG7:
            continue
        row = rs.compute_row(s['symbol'], s.get('pm_price'), s.get('prev_close'), bench_pct, thr)
        row['spot'] = s.get('pm_price')
        rows.append(row)
        s['rs'], s['rs_class'], s['rs_tag'] = row['rs'], row['rs_class'], row['rs_tag']
    divs = rs.divergences(rows, thr)
    if divs:
        logger.info("RS divergences vs %s: %s", config.RS_BENCHMARK,
                    ", ".join(f"{d['symbol']} {d['rs']:+.2f}({d['rs_tag']})" for d in divs))
    try:
        db.save_relative_strength(rows, scope='MORNING', bench_symbol=config.RS_BENCHMARK,
                                  bench_pct=bench_pct, bench_spot=bench_spot,
                                  session_date=today, ts=now)
    except Exception:
        logger.warning("Morning RS save failed", exc_info=True)
    return benchmarks, divs


def morning_snapshot(schwab: SchwabClient, sheets: SheetsLogger, adata=None) -> None:
    """
    Run the daily pre-market setup (config.SNAPSHOT_HOUR:MINUTE CST, default 08:10).
    Option chains/OI come from Schwab (Alpaca has no
    live OI); the SPOT anchor for S/R levels + sentiment prefers the Alpaca SIP quote
    mid (freshest pre-market price), falling back to Schwab, then prev close.
    """
    now   = now_cst()
    today = today_cst()
    logger.info("═══ MORNING SNAPSHOT START (%s) ═══", now.strftime('%Y-%m-%d %H:%M CST'))

    sentiments:    list[dict] = []
    all_oi_buildup: list[dict] = []   # top OI_BUILDUP rows across all symbols → briefing
    weekend_gaps:  list[dict] = []    # per-symbol weekend/holiday OI gaps → briefing

    for symbol in config.SYMBOLS:
        try:
            prev_close = schwab.get_prev_close(symbol)

            # 8:20 SPOT anchor — prefer the Alpaca SIP bid/ask mid (freshest pre-market
            # price), then the Schwab quote, then prev_close as a last resort. Using the
            # quote mid avoids silently anchoring to a stale last-trade / yesterday's close.
            pm_price, src = None, None
            if adata is not None:
                try:
                    pm_price = adata.get_quote_mid(symbol)
                    src = 'alpaca-mid'
                except Exception:
                    pm_price = None
            if not pm_price:
                try:
                    pm_price = schwab.get_quote(symbol).get('price')
                    src = 'schwab'
                except Exception:
                    pm_price = None
            if not pm_price:
                pm_price, src = prev_close, 'prev_close(fallback)'

            logger.info("%s: prev_close=%.4f  spot=%.4f  (anchor=%s)",
                        symbol, prev_close, pm_price, src)

            chain  = schwab.get_option_chain(symbol)
            expiry = chain['expiry']

            # OI levels anchored to the 8:20 AM spot price (pre-market)
            levels = compute_oi_levels(chain, pm_price)

            # Top-3 OI snapshot
            snap = get_top_oi_snapshot(chain, pm_price)

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
                underlying_price=pm_price,
            )
            # Phase 0: compute oi_change vs prior session for every contract (§17)
            try:
                db.reconcile_oi_changes(symbol, today)
            except Exception:
                logger.warning("%s: OI change reconciliation failed", symbol, exc_info=True)

            # §37 Next-day intent reconciliation: update prior-day oi_events with the
            # overnight OI change now visible in today's option_chain_snapshots.
            try:
                db.reconcile_prior_oi_events(symbol, today)
            except Exception:
                logger.warning("%s: OI event reconciliation failed", symbol, exc_info=True)

            db.save_oi_levels(symbol, today, now, levels)
            db.save_morning_sentiment(symbol, today, sentiment['pc_ratio'], sentiment['bias'], now)

            # §11-§16 Secondary OI Watchlist — extended ranks, outer wall, OI buildup.
            # Runs after reconcile_oi_changes so OI_BUILDUP tier has overnight deltas.
            try:
                oi_changes = db.get_oi_changes_today(symbol, today)
                secondary  = compute_secondary_watchlist(chain, pm_price, oi_changes)
                db.save_secondary_oi_levels(symbol, today, now, secondary)
                for row in secondary:
                    if (row['watchlist_tier'] == 'OI_BUILDUP'
                            and (row.get('oi_change') or 0) > 0):
                        all_oi_buildup.append({'symbol': symbol, **row})
            except Exception:
                logger.warning("%s: secondary OI watchlist failed", symbol, exc_info=True)

            # ── Weekend / post-holiday OI gaps (near-dated, multi-expiry) ──
            # Snapshot every near-dated expiry daily into the isolated
            # near_oi_snapshots table so the prior session is always on hand; on
            # the first session after a multi-day market closure (gap >= 2 days),
            # surface the biggest OI changes since then in the briefing.
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
                    logger.warning("%s: weekend OI gap detection failed", symbol, exc_info=True)

            # ── 1-hour option bars for the 6 S/R level contracts (Alpaca) ──
            # Recent hourly price/volume history for each watched OI contract,
            # pulled once per day alongside the briefing. Alpaca-only (Schwab
            # serves no option price-history); skipped if no Alpaca data client.
            if adata is not None and config.COLLECT_HOURLY_OPTION_BARS:
                try:
                    _collect_hourly_option_bars(symbol, levels, expiry, today, adata)
                except Exception:
                    logger.warning("%s: hourly option-bar collection failed", symbol, exc_info=True)

            # ── Log to Google Sheets ──
            sheets.log_daily_levels(symbol, levels, pm_price, now)
            sheets.log_oi_snapshot(
                symbol=symbol,
                expiry=expiry,
                top_calls=snap['top_calls'],
                top_puts=snap['top_puts'],
                underlying_price=pm_price,
                snap_time=now,
            )
            sheets.log_morning_sentiment(sentiment, now)
            sheets.log_comparison_row(
                symbol=symbol,
                expiry=expiry,
                underlying_price=pm_price,
                levels=levels,
                snap=snap,
                computed_at=now,
            )

        except Exception:
            logger.exception("Morning snapshot failed for %s", symbol)

    # Relative strength vs QQQ (adds rs/rs_class/rs_tag to each MAG7 sentiment).
    benchmarks, rs_divergences = _compute_morning_relative_strength(
        sentiments, schwab, adata, today, now)

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
    top_buildup = sorted(all_oi_buildup, key=lambda x: x.get('oi_change') or 0, reverse=True)[:5]

    # Open-position check: surface anything Alpaca still holds into the session
    # (normally flat post-EOD; non-empty flags a carryover/orphan to review).
    open_pos = None
    if config.BRIEFING_CHECK_OPEN_POSITIONS:
        try:
            open_pos = collect_open_positions(AlpacaClient(), today)
        except Exception:
            logger.warning("Morning briefing: open-position check failed", exc_info=True)

    discord_briefing(discord_results, now, oi_buildup=top_buildup,
                     weekend_gaps=weekend_gaps, open_positions=open_pos,
                     benchmarks=benchmarks, rs_divergences=rs_divergences)

    # Retention: keep only the most recent N trading days of 1-min bar data.
    # Runs once per trading day here; alerts/signals are never pruned.
    try:
        db.prune_old_bars(config.BAR_RETENTION_DAYS)
    except Exception:
        logger.warning("Bar retention prune failed", exc_info=True)

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
                cells.append("  - ")
        return cells

    for s in mag7_rows:
        sign = "+" if s['pm_change_pct'] >= 0 else ""
        lvls = s.get('levels', [])
        # Display by proximity: support nearest = highest strike, resistance
        # nearest = lowest strike. Rank 1 = nearest; '*' = dominant OI wall.
        # (The persisted `rank` is OI-based and unchanged — gates are unaffected.)
        s1, s2, s3 = _prox_cells([l for l in lvls if l['level_type'] == 'SUPPORT'],    reverse=True)
        r1, r2, r3 = _prox_cells([l for l in lvls if l['level_type'] == 'RESISTANCE'], reverse=False)
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

def _last_closed_opt_bar_vol(data_src, occ: str, bar_time) -> Optional[int]:
    """
    Volume of the most recent CLOSED 1-min OPRA bar for `occ`, strictly before the
    current minute — the §7-8 completed-bar source for the production volume gate.
    Returns None on no data / error so the gate falls back to the live poll-delta.
    """
    try:
        cur_min = bar_time.replace(second=0, microsecond=0)
        bars = data_src.get_option_bars(occ)
        closed = [int(b['volume']) for b in bars if b['bar_time'] < cur_min]
        return closed[-1] if closed else None
    except Exception:
        return None


_rs_diverged_today: set = set()   # (symbol, date) already logged as an intraday divergence


def _session_pct(session_bars: list, live_spot=None):
    """% change from the first session bar to the live spot (or last close). (pct, ref_spot)."""
    if not session_bars:
        return None, None
    ref = session_bars[0].get('close')
    cur = live_spot if live_spot is not None else session_bars[-1].get('close')
    return rs.pct_change(cur, ref), cur


def _session_vwap(bars: list):
    """Volume-weighted average price over the session bars (typical price × volume), or None."""
    num = den = 0.0
    for b in bars or []:
        v = b.get('volume') or 0
        c = b.get('close')
        if c is None or v <= 0:
            continue
        tp = (b.get('high', c) + b.get('low', c) + c) / 3.0
        num += tp * v
        den += v
    return round(num / den, 4) if den > 0 else None


def _qqq_intraday_context(dbc, data_src):
    """QQQ (RS_BENCHMARK) % change since the session open + current spot; None if off/unavailable."""
    if not config.RELATIVE_STRENGTH_ENABLED:
        return None
    bench = config.RS_BENCHMARK
    try:
        sb = data_src.get_bars(bench, count=config.SESSION_BARS) if data_src else (dbc.get_bars(bench) if dbc else None)
        if not sb:
            return None
        spot = sb[-1].get('close')
        if data_src:
            try:
                q = data_src.get_quote(bench)
                if q and q.get('price'):
                    spot = float(q['price'])
            except Exception:
                pass
        pct, _ = _session_pct(sb, spot)
        return {'pct': pct, 'spot': spot}
    except Exception:
        logger.warning("%s intraday context fetch failed", bench, exc_info=True)
        return None


def _record_intraday_rs(symbol, session_bars, live_spot, qqq_ctx, today, now) -> None:
    """Compute + persist one symbol's intraday relative strength vs QQQ; log/alert on
    the first hard divergence of the day (Discord gated by RS_INTRADAY_DISCORD_ALERT)."""
    spct, _ = _session_pct(session_bars, live_spot)
    if spct is None or qqq_ctx.get('pct') is None:
        return
    thr = config.RS_INTRADAY_DIVERGENCE_PCT
    r   = rs.relative_strength(spct, qqq_ctx['pct'])
    cls = rs.classify_rs(r, thr)
    row = {'symbol': symbol, 'pct': spct, 'rs': r, 'rs_class': cls, 'spot': live_spot}
    try:
        db.save_relative_strength([row], scope='INTRADAY', bench_symbol=config.RS_BENCHMARK,
                                  bench_pct=qqq_ctx['pct'], bench_spot=qqq_ctx.get('spot'),
                                  session_date=today, ts=now)
    except Exception:
        logger.warning("%s intraday RS save failed", symbol, exc_info=True)
    if cls in ('RELATIVELY_STRONG', 'RELATIVELY_WEAK') and (symbol, today) not in _rs_diverged_today:
        _rs_diverged_today.add((symbol, today))
        logger.info("RS-INTRADAY %s %s vs %s: %+.2f%%  (own %+.2f%% · %s %+.2f%%)",
                    symbol, cls, config.RS_BENCHMARK, r, spct, config.RS_BENCHMARK, qqq_ctx['pct'])
        if config.RS_INTRADAY_DISCORD_ALERT:
            try:
                from output.discord_notifier import send_rs_divergence_alert
                send_rs_divergence_alert(symbol, r, spct, qqq_ctx['pct'], cls, config.RS_BENCHMARK)
            except Exception:
                logger.warning("RS divergence Discord failed", exc_info=True)


def intraday_check(
    dbc: DatabentoClient,
    detector: SignalDetector,
    monitor: PositioningMonitor,
    sheets: SheetsLogger,
    data_src=None,
    alpaca: Optional[AlpacaClient] = None,
) -> None:
    """
    Scan all symbols once per 60-second poll: pull bars, run the signal detector,
    fire desktop notifications, and update the positioning monitor.  Failures for
    individual symbols are logged without interrupting the remaining symbols.

    `data_src` is the intraday market-data source (AlpacaDataClient in production;
    any object exposing get_bars/get_quote/get_nearest_expiry/get_watched_contracts/
    get_option_history_range/get_option_bars works). Its real-time bid/ask populate
    price_to_enter / price_to_exit. Databento (`dbc`) is the fallback.
    """
    today = today_cst()

    # QQQ benchmark context for relative strength (fetched once per cycle; context only).
    qqq_ctx = _qqq_intraday_context(dbc, data_src)

    for symbol in config.SYMBOLS:
        try:
            # 1-min equity bars — primary data source, Databento fallback.
            # The primary pulls the full session so cumulative volume is correct;
            # the signal detector still sees only the trailing BARS_TO_FETCH slice.
            if data_src:
                session_bars = data_src.get_bars(symbol, count=config.SESSION_BARS)
                bars = session_bars[-config.BARS_TO_FETCH:] if session_bars else []
            elif dbc:
                bars = dbc.get_bars(symbol)
                session_bars = bars
            else:
                logger.warning("%s: no data source available for bars", symbol)
                continue

            if not bars:
                logger.warning("%s: no bars returned", symbol)
                continue

            # ── Staleness guard — never act on stale/previous-session data ──
            latest_bt = bars[-1]['bar_time']
            age_sec   = (now_cst() - latest_bt).total_seconds()
            if latest_bt.date() != today or age_sec > config.MAX_BAR_AGE_SECONDS:
                logger.warning(
                    "%s: STALE bars — latest %s (%.0fs old) — skipping symbol",
                    symbol, latest_bt.strftime('%Y-%m-%d %H:%M CST'), age_sec,
                )
                continue

            # Persist 7 fields/bar: OHLC, volume (per-min), spot_price, cum_volume.
            # cum_volume is only valid on the full-session pull (data_src); the
            # Databento rolling buffer is partial → store NULL cum_volume.
            db.save_bars(symbol, session_bars, full_session=bool(data_src))

            # ── Live spot from a real-time quote (freshest tick) ──
            # The last *completed* candle lags up to a minute; use the live last
            # price for the spot and fall back to the bar close only on failure.
            underlying_price = bars[-1]['close']
            if data_src:
                try:
                    q = data_src.get_quote(symbol)
                    if q and q.get('price'):
                        underlying_price = float(q['price'])
                except Exception:
                    logger.warning("%s: live quote failed, using last bar close", symbol)

            # The detector reads the latest bar's close as spot — feed it the live
            # spot (persisted bars keep their true candle close).
            bars = bars[:-1] + [{**bars[-1], 'close': underlying_price}]

            # ── Relative strength vs QQQ (monitor alongside MAG7; context only) ──
            if qqq_ctx:
                _record_intraday_rs(symbol, session_bars, underlying_price, qqq_ctx, today, now_cst())

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
                if data_src:
                    expiry = data_src.get_nearest_expiry(symbol)
                elif dbc:
                    expiry = dbc.get_nearest_expiry(symbol)
                if data_src and expiry:
                    # n=3 so a genuine ATM + in-the-money strike pair is available
                    option_quotes = data_src.get_watched_contracts(
                        symbol, expiry, underlying_price, n=3
                    )
                    logger.debug(
                        "%s: watched contracts near %.2f — %d quotes (expiry %s)",
                        symbol, underlying_price, len(option_quotes), expiry,
                    )
                elif dbc and expiry:
                    option_quotes = dbc.get_option_quotes_for_levels(
                        symbol, expiry, levels
                    )
            except Exception:
                logger.warning("%s: option quote fetch failed, proceeding without", symbol)

            # Per-minute 1-min OHLCV + Greeks for the 6 S/R level option contracts
            if data_src and config.COLLECT_LEVEL_BARS and expiry:
                try:
                    _collect_level_bars(symbol, levels, expiry, today, data_src,
                                        option_quotes=option_quotes)
                except Exception:
                    logger.warning("%s: level option-bar collection failed", symbol, exc_info=True)

            # Morning P/C ratio for conviction multiplier
            pc_ratio = db.get_today_pc_ratio(symbol, today)

            # §13 historical-value gate: let the detector fetch a contract's
            # multi-day (low, high) on demand (daily option candles), cached per day.
            hist_range_fn = (data_src.get_option_history_range
                             if (data_src and config.HIST_LOW_ENTRY_GATE) else None)
            # §7-8: closed 1-min OPRA bar volume for the production gate's partial→
            # completed re-evaluation (None when no Alpaca data client is available).
            completed_bar_fn = ((lambda occ, bt: _last_closed_opt_bar_vol(data_src, occ, bt))
                                if data_src else None)
            signals = detector.check(symbol, bars, levels, option_quotes, expiry=expiry,
                                     pc_ratio=pc_ratio,
                                     opening_range=is_opening_range(), hist_range_fn=hist_range_fn,
                                     fired_today_fn=db.get_fired_directions_today,
                                     prev_range_fn=(db.get_option_hist_range
                                                    if config.HIST_LOW_ENTRY_GATE else None),
                                     completed_bar_fn=completed_bar_fn)

            # §73 — persist every candidate evaluation (blocked + passed), not just alerts.
            if detector.last_candidates:
                try:
                    db.save_signal_candidates(detector.last_candidates, bars[-1]['bar_time'], today)
                except Exception:
                    logger.warning("%s: candidate logging failed", symbol, exc_info=True)

            # Per-minute call/put leadership snapshot — computed once, reused for the
            # intent observations (below) and the §41 stored series.
            poll_leadership = None
            if option_quotes:
                try:
                    poll_leadership = compute_leadership_scores(
                        symbol, option_quotes, detector._opt_vol_hist,
                        low_dist_fn=detector._contract_low_dist)
                except Exception:
                    logger.warning("%s: volume leadership scoring failed", symbol, exc_info=True)

            # §4 merge primary+chain into one signal (only when Gold-mode is on;
            # the one-per-direction dedup already prevents same-side duplicates).
            if config.GOLD_ONLY_PRODUCTION_MODE:
                signals = gold_mode.merge(signals)

            gold_intent = (config.GOLD_ONLY_PRODUCTION_MODE
                           and config.INTENT_VALIDATION_ENABLED)

            if gold_intent:
                # §4-§9 Deferred Gold entry: a candidate is NOT alerted on its event
                # bar — it registers PENDING and is promoted only when the next 1-3
                # bars confirm directional demand and the opposite side does not veto.
                def _obs(side, strike):
                    q = option_quotes.get((float(strike), side), {}) if option_quotes else {}
                    return {'mark': q.get('mark'), 'iv': q.get('implied_vol'),
                            'spot': underlying_price,
                            'call_leadership': (poll_leadership or {}).get('call_leadership', 0.0),
                            'put_leadership':  (poll_leadership or {}).get('put_leadership', 0.0)}
                stepped = _intent_gate.step(symbol, _obs)              # advance pending
                routed  = _intent_gate.classify_new(symbol, signals, _obs)
                for sig in routed['emit'] + stepped['emit']:
                    gold_mode.annotate_and_gate(sig)                  # stamp production fields
                    sid = _persist_signal(sig, sheets)
                    _emit_production(sig, sid, alpaca, sheets)
                for sig in routed['research'] + stepped['research']:
                    gold_mode.annotate_and_gate(sig)
                    _persist_signal(sig, sheets)
                    logger.info("GOLD-MODE research-only: %s %s grade=%s intent=%s veto=%s",
                                symbol, sig.get('signal_type'), sig.get('gold_grade'),
                                sig.get('intent_class'), sig.get('opp_veto'))
                if routed['deferred']:
                    logger.info("GOLD-MODE deferred %d candidate(s) awaiting intent: %s",
                                len(routed['deferred']), symbol)
            else:
                for sig in signals:
                    # §1/§18/§19 — Gold gate. Pass-through when the mode is off, so
                    # live behavior is unchanged until it is deliberately enabled.
                    production_ok = gold_mode.annotate_and_gate(sig)
                    sig_id = _persist_signal(sig, sheets)
                    if not production_ok:
                        logger.info("GOLD-MODE research-only: %s %s ctx=%s grade=%s vr=%s clow=%s",
                                    symbol, sig.get('signal_type'), sig.get('signal_context'),
                                    sig.get('gold_grade'), sig.get('value_region'),
                                    sig.get('clow_region'))
                        continue                      # §19: no Discord, no paper trade
                    _emit_production(sig, sig_id, alpaca, sheets)

            # §41 Per-minute call/put leadership scores (stored for all symbols every poll).
            if poll_leadership:
                try:
                    db.save_volume_leadership(symbol, bars[-1]['bar_time'],
                                              today, underlying_price, poll_leadership)
                except Exception:
                    logger.warning("%s: volume leadership save failed", symbol, exc_info=True)

            # Exit monitoring — check R1/R2 or S1/S2 targets for open trades
            if alpaca and config.ALPACA_ENABLED:
                check_exits(symbol, underlying_price, alpaca, now_cst(), sheets, option_quotes, detector,
                            bars=session_bars)

            # Volume cluster positioning monitor (Postgres only, no signals)
            if dbc:
                try:
                    expiry_pair = dbc.get_expiry_pair(symbol)
                    atm_quotes  = dbc.get_atm_option_quotes_all_expiries(symbol, underlying_price)
                    monitor.update(symbol, atm_quotes, expiry_pair, levels, underlying_price)
                except Exception:
                    logger.warning("%s: positioning monitor update failed", symbol, exc_info=True)

        except Exception:
            logger.exception("Intraday check failed for %s", symbol)


def _collect_level_bars(symbol, levels, expiry, level_date, data_src,
                        option_quotes: dict = None) -> None:
    """
    Pull and persist 1-min OHLCV + Greeks for each S/R level's option contract.

    Greeks (delta/gamma/vega/theta/rho/IV) are sourced from option_quotes (Alpaca
    OPRA snapshots, which include greeks + impliedVolatility) and attached to the
    forming (most recent) bar only.  Historical bars are re-upserted with NULL Greeks;
    the COALESCE logic in save_option_level_bars preserves any previously stored
    Greek values so they are never overwritten with NULL.

    cum_option_volume is the running session volume total for the contract,
    recomputed across all bars each poll so completed bars self-correct.
    """
    # Build a Greeks lookup from option_quotes keyed by (strike, option_type).
    # option_quotes covers the nearest n strikes to spot; farther S/R levels
    # get NULL Greeks until spot moves within range.
    greeks_by_contract: dict = {}
    if option_quotes:
        for (strike, opt_type), q in option_quotes.items():
            greeks_by_contract[(strike, opt_type)] = {
                'implied_vol': q.get('implied_vol'),
                'delta':       q.get('delta'),
                'gamma':       q.get('gamma'),
                'vega':        q.get('vega'),
                'theta':       q.get('theta'),
                'rho':         q.get('rho'),
            }

    rows: list[dict] = []
    for lv in levels:
        strike      = float(lv['strike'])
        option_type = lv['option_type']
        occ         = occ_symbol(symbol, expiry, strike, option_type)
        obars       = data_src.get_option_bars(occ, count=config.SESSION_BARS)
        if not obars:
            continue

        current_greeks = greeks_by_contract.get((strike, option_type), {})
        cum = 0
        for i, b in enumerate(obars):
            cum += b['volume']
            # Greeks snapshot is only available for the forming (last) bar;
            # historical bars keep whatever was stored during their forming minute.
            g = current_greeks if i == len(obars) - 1 else {}
            rows.append({
                'symbol':            symbol,
                'level_date':        level_date,
                'level_type':        lv['level_type'],
                'rank':              lv['rank'],
                'strike':            strike,
                'option_type':       option_type,
                'expiry':            expiry,
                'occ_symbol':        occ,
                'bar_time':          b['bar_time'],
                'open':              b['open'],
                'high':              b['high'],
                'low':               b['low'],
                'close':             b['close'],
                'volume':            b['volume'],
                'cum_option_volume': cum,
                'implied_vol':       g.get('implied_vol'),
                'delta':             g.get('delta'),
                'gamma':             g.get('gamma'),
                'vega':              g.get('vega'),
                'theta':             g.get('theta'),
                'rho':               g.get('rho'),
            })

    db.save_option_level_bars(rows)
    logger.debug("%s: saved %d option-level bars across %d levels", symbol, len(rows), len(levels))


def _collect_hourly_option_bars(symbol, levels, expiry, snap_date, adata) -> None:
    """
    Pull and persist OPT_HOURLY_LOOKBACK_DAYS of 1-hour OHLCV for each S/R level's
    option contract (Alpaca). Runs once per day in the morning snapshot, giving the
    6 watched OI contracts recent hourly price/volume history for analysis/backtests.
    Best-effort per contract: one contract's failure never blocks the others.
    """
    rows: list[dict] = []
    for lv in levels:
        strike      = float(lv['strike'])
        option_type = lv['option_type']
        occ         = occ_symbol(symbol, expiry, strike, option_type)
        try:
            hbars = adata.get_option_hourly_bars(occ)
        except Exception:
            logger.warning("%s: hourly option-bar fetch failed for %s", symbol, occ, exc_info=True)
            continue
        for b in hbars:
            rows.append({
                'symbol':      symbol,
                'snap_date':   snap_date,
                'level_type':  lv['level_type'],
                'rank':        lv['rank'],
                'strike':      strike,
                'option_type': option_type,
                'expiry':      expiry,
                'occ_symbol':  occ,
                'bar_time':    b['bar_time'],
                'open':        b['open'],
                'high':        b['high'],
                'low':         b['low'],
                'close':       b['close'],
                'volume':      b['volume'],
            })

    db.save_option_hourly_bars(rows)
    logger.info("%s: saved %d hourly option bars across %d levels",
                symbol, len(rows), len(levels))


# ── Desktop notification ──────────────────────────────────────────────────────

def _persist_signal(sig: dict, sheets: SheetsLogger) -> int:
    """Persist a signal (emergent FK + DB + Sheets + logged flag); return its id."""
    emergent = sig.pop('emergent', None)
    if emergent is not None:
        sig['emergent_location_id'] = db.save_emergent_location(emergent)
    event_state = sig.pop('event_state', None)   # P-ET: not a signals column
    gate_audit = sig.pop('gate_audit', None)     # §13: not a signals column
    sig_id = db.save_signal(sig)
    if gate_audit is not None:
        summary = gold_mode.audit_summary(gate_audit)
        if gate_audit.get('decision') == 'RESEARCH':
            logger.info("GATE-AUDIT %s %s: %s", sig.get('symbol'),
                        sig.get('signal_type'), summary)
        try:
            db.save_signal_gate_audit(sig_id, sig.get('symbol'), gate_audit, summary)
        except Exception:
            logger.warning("save_signal_gate_audit failed for %s", sig.get('symbol'), exc_info=True)
    if event_state is not None:
        # §1/§13 stamp commit-time quotes + realistic paper fill + price-moved flag.
        b, a = sig.get('opt_bid'), sig.get('opt_ask')
        event_state.bid_at_commit = b
        event_state.ask_at_commit = a
        event_state.mid_at_commit = round((b + a) / 2, 4) if (b and a) else None
        event_state.paper_fill_price = sig.get('price_to_enter')
        event_state.paper_fill_method = sig.get('paper_fill_method')
        event_state.price_moved_from_event = price_moved_from_event(
            sig.get('price_to_enter'), event_state.last_at_threshold)
        if event_state.price_moved_from_event:
            logger.info("PRICE-MOVED %s %s: fill=%s far from event ref=%s",
                        sig.get('symbol'), sig.get('option_type'),
                        sig.get('price_to_enter'), event_state.last_at_threshold)
        # §17 latency — stamp commit time on the same CST bar clock, expose the
        # profile on the sig for the Discord card, and persist it.
        event_state.commit_time = now_cst()
        sig['latency'] = event_state.latency_profile()
        try:
            db.save_signal_event_state(sig_id, event_state)
            db.save_signal_latency(sig_id, event_state)
        except Exception:
            logger.warning("save_signal_event_state failed for %s", sig.get('symbol'), exc_info=True)
    sheets.log_signal(sig)
    db.mark_signal_logged(sig_id)
    return sig_id


def _emit_production(sig: dict, sig_id: int, alpaca, sheets: SheetsLogger) -> None:
    """Fire the production actions for a signal: Discord + (non-WATCH/non-upgrade) trade."""
    _notify_signal(sig)
    if (alpaca and config.ALPACA_ENABLED
            and sig.get('confidence') != 'WATCH'
            and not sig.get('upgrade')):
        _execute_trade(sig, sig_id, alpaca, sheets)


def _notify_signal(sig: dict) -> None:
    """Best-effort desktop + Discord notification when a signal fires.

    WATCH alerts are still recorded (DB + Sheets) by the caller but are not sent
    to Discord unless DISCORD_NOTIFY_WATCH is on — this avoids a WATCH heads-up
    and the subsequent real entry showing as two Discord messages for the same
    ticker/direction.
    """
    if sig.get('confidence') == 'WATCH' and not config.DISCORD_NOTIFY_WATCH:
        logger.debug("%s %s WATCH — Discord muted (DISCORD_NOTIFY_WATCH off)",
                     sig.get('symbol'), sig.get('signal_type'))
    else:
        try:
            discord_signal(sig)
        except Exception:
            logger.warning("Discord signal send failed", exc_info=True)

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

def _execute_trade(sig: dict, sig_id: int, alpaca: AlpacaClient, sheets: SheetsLogger) -> None:
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
    # The traded contract is the ATM strike at/near the level (no OTM shift).
    strike      = sig.get('traded_strike') or sig.get('level_price')
    option_type = sig.get('option_type')
    signal_type = sig.get('signal_type', '')

    if not price:
        logger.warning("Alpaca: trade skipped for %s — no price_to_enter", symbol)
        return
    if not expiry:
        logger.warning("Alpaca: trade skipped for %s — no expiry in signal", symbol)
        return

    # Atomic position cap. Alpaca's position count lags newly-placed orders, so a
    # burst of signals in one poll cycle could each see "room" and blow past the
    # cap (this opened 5 positions vs a max of 3 on 06-02). Count the DB's open
    # trades too — those are written the instant each order is placed and cleared
    # on close — and take the max, so the cap holds even before Alpaca registers
    # the fills.
    open_pos = max(alpaca.open_position_count(), db.count_open_trades())
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

    # ── Exit targets: use the signal's own shifted targets (skip-nearest rule).
    # Fall back to recomputing them with the same rule only if the signal didn't
    # carry them. ──
    exit1_underlying = sig.get('exit1_price')
    exit2_underlying = sig.get('exit2_price')
    if exit1_underlying is None:
        try:
            levels = db.get_today_levels(symbol, today_cst())
            spot   = float(sig.get('trigger_price') or strike)
            exit1_underlying, exit2_underlying = compute_exit_targets(
                signal_type, spot, levels, position_only=False,
            )
        except Exception:
            logger.warning("Alpaca: could not resolve exit targets for %s", symbol, exc_info=True)

    # No defined exit target → no risk-managed exit → don't enter (alert only).
    if exit1_underlying is None:
        logger.warning("Alpaca: trade skipped for %s — no exit target available", symbol)
        return

    # Split the position half/half across the two targets. A 1-contract position
    # cannot be split (qty//2 == 0, which Alpaca rejects as "qty must be > 0"), so
    # exit the whole thing at target 1 and leave the second leg empty.
    if qty < 2:
        exit1_qty, exit2_qty = qty, 0
    else:
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
            'stoploss_price':    None,   # no 50% premium stop at entry — it whipsawed us out of winners (0DTE premium noise). Stop arms to breakeven only after Exit 1.
            'strike':            strike,
            'option_type':       option_type,
            'expiry':            expiry,
        })
        sheets.log_trade_entry(order, sig, qty, spend)


_reversal_counts: dict = {}   # (symbol, date) -> flips today (anti-churn cap)


def _reversal_under_cap(symbol, now) -> bool:
    key = (symbol, now.date())
    return _reversal_counts.get(key, 0) < config.REVERSAL_MAX_PER_DAY


def _flip_entry(symbol, rev, spot, expiry, alpaca, sheets, now, option_quotes):
    """
    Open the opposite-side paper trade with its own R2/R3 (calls) or S2/S3 (puts)
    targets — the recursive 'story change' flip. Builds a synthetic reversal signal,
    persists it, routes it through _execute_trade (same targets/qty/order/exit path),
    and emits the entry alert. Returns True if an order was placed.
    """
    opp_type   = rev['opp_type']                          # 'CALL' | 'PUT'
    new_type   = 'BULLISH' if opp_type == 'CALL' else 'BEARISH'
    best       = rev.get('opp_best')
    if not best or best.get('strike') is None:
        return False
    strike = float(best['strike'])
    q      = option_quotes.get((strike, opp_type)) or {}
    price  = q.get('ask') or q.get('mark')
    if not price:
        return False
    expiry = expiry or today_cst()

    levels = db.get_today_levels(symbol, today_cst())
    e1, e2 = compute_exit_targets(new_type, spot, levels)
    if e1 is None:
        logger.info("Reversal flip %s %s: no exit targets — skipping flip entry", symbol, opp_type)
        return False

    # Nearest originating level for the alert's "Level" line.
    if new_type == 'BULLISH':
        cand = sorted([l for l in levels if l['level_type'] == 'SUPPORT' and float(l['strike']) <= spot],
                      key=lambda l: spot - float(l['strike']))
        lvl_type = 'SUPPORT'
    else:
        cand = sorted([l for l in levels if l['level_type'] == 'RESISTANCE' and float(l['strike']) >= spot],
                      key=lambda l: float(l['strike']) - spot)
        lvl_type = 'RESISTANCE'
    lvl   = cand[0] if cand else None
    label = (('S' if new_type == 'BULLISH' else 'R') + str(lvl['rank'])) if lvl else ''
    evb   = best.get('ev') or {}

    sig = {
        'symbol': symbol, 'signal_time': now, 'signal_type': new_type,
        'bias': 'Call-side bias' if new_type == 'BULLISH' else 'Put-side bias',
        'level_type': lvl_type, 'level_price': float(lvl['strike']) if lvl else strike,
        'level_label': label, 'trigger_price': round(float(spot), 4),
        'option_type': opp_type, 'opt_mark': q.get('mark'), 'opt_bid': q.get('bid'),
        'opt_ask': q.get('ask'), 'price_to_enter': round(price, 2),
        'price_to_exit': round(price * 2, 2), 'prox_score': 1.0,
        'cluster_strength': None, 'strong_cluster': False,
        'flow_shape': 'REVERSAL', 'signal_shape': 'REVERSAL', 'confidence': 'REVERSAL',
        'upgrade': False, 'cluster_active_bars': None, 'cluster_burst_bars': None,
        'day_mode': '0DTE', 'traded_strike': strike, 'target_level': None,
        'atm_vol_1m': None, 'atm_spike_ratio': None, 'atm_vol_3m': None,
        'itm_vol_1m': None, 'itm_spike_ratio': None, 'itm_vol_3m': None,
        'spread_pct': None, 'low_dist': best.get('low_dist'), 'room_score': None,
        'room_pct': None, 'pc_ratio': None, 'pc_conviction': None, 'option_hl_flag': None,
        'opt_vol_delta': None, 'avg_volume_20': None, 'spike_volume': None,
        'consecutive_spikes': None, 'expiry': expiry, 'exit1_price': e1, 'exit2_price': e2,
        # Discord trigger display (sourced from the opposite-side event that flipped us)
        'trigger_volume_type': 'FIVE_BAR_WINDOW', 'trigger_volume': evb.get('event_vol'),
        'trigger_ratio': evb.get('burst'),
    }
    try:
        sig_id = db.save_signal(sig)
    except Exception:
        logger.warning("Reversal flip: save_signal failed", exc_info=True)
        sig_id = None
    before = db.count_open_trades()
    _execute_trade(sig, sig_id, alpaca, sheets)
    placed = db.count_open_trades() > before
    if placed:
        _notify_signal(sig)                                # new entry alert (with targets)
        _reversal_counts[(symbol, now.date())] = _reversal_counts.get((symbol, now.date()), 0) + 1
        logger.info("Reversal FLIP entry: %s %s @ %.2f  targets %s/%s",
                    symbol, opp_type, price, e1, e2)
    return placed


def _handle_reversal(trade, occ, symbol, spot, rev, alpaca, now, sheets, option_quotes) -> None:
    """
    Confirmed flow-leadership reversal (spec §8): exit the current position, then —
    if FLOW_REVERSAL_AUTO_FLIP — OPEN the opposite paper trade with its own R2/R3 or
    S2/S3 targets (recursive story change). Always records the reversal + the
    opposite (hypothetical-or-real) entry in flow_reversals and alerts Discord.
    """
    pos_type  = trade['option_type']
    remaining = trade['exit2_qty'] if trade.get('exit1_filled') else trade['qty']
    exit_q     = option_quotes.get((float(trade['strike']), pos_type)) if trade.get('strike') else None
    exit_price = (exit_q or {}).get('mark')

    order = alpaca.close_position_qty(occ, remaining)
    if order:
        db.mark_trade_stopped(trade['id'], now)
        sheets.log_trade_exit(order, dict(trade), f"FLOW REVERSAL -> {rev['opp_type']}", spot)

    best        = rev.get('opp_best')
    hypo_strike = best['strike'] if best else None
    hypo_price  = best.get('mark') if best else None
    hypo_occ    = (occ_symbol(symbol, trade.get('expiry') or today_cst(), hypo_strike, rev['opp_type'])
                   if hypo_strike else None)

    # Story change → open the opposite side with its own targets (else exit+alert only).
    flipped = False
    if config.FLOW_REVERSAL_AUTO_FLIP and _reversal_under_cap(symbol, now):
        try:
            flipped = _flip_entry(symbol, rev, spot, trade.get('expiry'), alpaca, sheets, now, option_quotes)
        except Exception:
            logger.warning("reversal flip-entry failed", exc_info=True)
    elif config.FLOW_REVERSAL_AUTO_FLIP:
        logger.info("Reversal %s: per-day flip cap reached (%d) — exit+alert only",
                    symbol, config.REVERSAL_MAX_PER_DAY)

    revrow = dict(
        symbol=symbol, detected_at=now, trade_id=trade['id'],
        from_side=pos_type, to_side=rev['opp_type'], spot=round(float(spot), 4),
        exit_occ=occ, exit_price=exit_price,
        same_leadership=rev['same_leadership'], opp_leadership=rev['opp_leadership'],
        leadership_diff=rev['leadership_diff'],
        opp_burst=(best['ev']['burst'] if best else None),
        opp_share=(best['ev']['share'] if best else None),
        hypo_occ=hypo_occ, hypo_strike=hypo_strike, hypo_entry_price=hypo_price,
        flipped=bool(flipped),
    )
    try:
        db.save_flow_reversal(revrow)
    except Exception:
        logger.warning("save_flow_reversal failed", exc_info=True)
    try:
        send_reversal_alert(revrow)
    except Exception:
        logger.warning("reversal alert failed", exc_info=True)
    logger.info("FLOW REVERSAL %s %s->%s: exited %s @ %s, %s %s @ %s",
                symbol, pos_type, rev['opp_type'], occ, exit_price,
                'FLIPPED to' if flipped else 'hypothetical', hypo_occ, hypo_price)


def check_exits(
    symbol: str,
    underlying_price: float,
    alpaca: AlpacaClient,
    now: datetime,
    sheets: SheetsLogger,
    option_quotes: dict,
    detector: SignalDetector,
    bars: Optional[list] = None,
) -> None:
    """
    For every open trade on this symbol:
      1. Stoploss — if option mark drops to/below stoploss_price, close the position.
      2. Exit 1   — close half at R1/S1; move stoploss to breakeven.
                    Then check opposite-side volume at the target level:
                    → If opposite-side active (TrueCluster-level): close remainder early.
                    → If not: hold remainder for Exit 2.
      3. Exit 2   — close remainder at R2/S2 price target, OR earlier if opposite-side
                    volume validates at the exit1 level in a subsequent bar.
    """
    trades = db.get_open_trades(symbol)
    if not trades:
        _reversal_engine.reset(symbol)        # fresh state for the next position
        return
    for trade in trades:
        sig_type = trade.get('signal_type', '')
        occ      = trade['occ_symbol']

        # ── Entry-fill guard ─────────────────────────────────────────────────
        # The entry is a limit day-order that may not have filled. If Alpaca
        # holds no contracts for this option there is nothing to exit — selling
        # would open an uncovered short (rejected every poll). Skip until the
        # position is actually held; a fully-exited leg shows qty 0 too, which
        # is fine (its exit flags are already set, so nothing re-fires).
        if alpaca.position_qty(occ) <= 0:
            logger.debug("%s: no position held (entry unfilled?) — skipping exits", occ)
            continue

        # ── Flow-leadership reversal (spec §19) — opposite side took control? ─
        if config.FLOW_REVERSAL_ENABLED and option_quotes and trade.get('option_type'):
            pos_type = trade['option_type']
            same_events, opp_events = [], []
            for (k, ot), q in option_quotes.items():
                item = {
                    'strike': k,
                    'ev': volume_event(list(detector._opt_vol_hist.get((symbol, k, ot), []))),
                    'low_dist': detector._contract_low_dist((symbol, k, ot), q),
                    'mark': q.get('mark'),
                }
                (same_events if ot == pos_type else opp_events).append(item)
            # V2 price confirmation: the underlying must validate the control shift —
            # a call position needs VWAP LOSS (price below VWAP), a put position needs
            # VWAP RECLAIM (price above VWAP). Only computed when the layer is on.
            price_confirmed = None
            if config.REVERSAL_PRICE_CONFIRM_ENABLED and bars:
                vwap = _session_vwap(bars)
                if vwap:
                    price_confirmed = ((underlying_price < vwap) if pos_type == 'CALL'
                                       else (underlying_price > vwap))
            rev = _reversal_engine.evaluate(symbol, pos_type, same_events, opp_events, now,
                                            price_confirmed=price_confirmed)
            if rev['state'] != 'ACTIVE':
                logger.info("REVERSAL %-17s %s  same_lead=%.2f opp_lead=%.2f diff=%.2f "
                            "fading=%s streak=%d window=%s prem=%s price=%s",
                            rev['state'], occ, rev['same_leadership'], rev['opp_leadership'],
                            rev['leadership_diff'], rev['same_fading'], rev['opp_streak'],
                            rev['window_ok'], rev['premium_confirmed'], rev['price_confirmed'])
            if rev['reversal_confirmed']:
                _handle_reversal(trade, occ, symbol, underlying_price, rev,
                                 alpaca, now, sheets, option_quotes)
                _reversal_engine.reset(symbol)
                continue

        # ── Stoploss check (option mark vs stored stoploss_price) ────────────
        stoploss_price = trade.get('stoploss_price')
        if stoploss_price is not None and option_quotes:
            strike   = trade.get('strike')
            opt_type = trade.get('option_type')
            if strike and opt_type:
                quote = option_quotes.get((float(strike), opt_type))
                if quote:
                    current_mark = quote.get('mark') or quote.get('ask') or 0
                    if current_mark and current_mark <= float(stoploss_price):
                        remaining_qty = trade['exit2_qty'] if trade['exit1_filled'] else trade['qty']
                        logger.info(
                            "Stoploss triggered  %s  mark=%.2f  stop=%.2f  qty=%d",
                            occ, current_mark, float(stoploss_price), remaining_qty,
                        )
                        order = alpaca.close_position_qty(occ, remaining_qty)
                        if order:
                            db.mark_trade_stopped(trade['id'], now)
                            label = f"Stoploss ${float(stoploss_price):.2f}"
                            sheets.log_trade_exit(order, dict(trade), label, underlying_price)
                        continue  # skip exit target checks for this trade

        # ── Exit 1 ───────────────────────────────────────────────────────────
        if not trade['exit1_filled'] and trade.get('exit1_underlying'):
            target = float(trade['exit1_underlying'])
            hit = (
                (sig_type == 'BULLISH' and underlying_price >= target) or
                (sig_type == 'BEARISH' and underlying_price <= target)
            )
            if hit:
                r1_label = 'R1' if sig_type == 'BULLISH' else 'S1'
                r2_label = 'R2' if sig_type == 'BULLISH' else 'S2'
                logger.info(
                    "Exit1 triggered  %s  spot=%.2f  target=%.2f  qty=%d",
                    occ, underlying_price, target, trade['exit1_qty'],
                )
                order = alpaca.close_position_qty(occ, trade['exit1_qty'])
                if order:
                    db.mark_exit1_filled(trade['id'], now)
                    new_stop = float(trade['limit_price'])
                    db.update_stoploss(trade['id'], new_stop)
                    stop_str = f"${new_stop:.2f}" if new_stop >= 1 else f"{int(new_stop * 100)}¢"
                    label    = f"Exit 1/2 @ {r1_label}  |  Stop → {stop_str} (breakeven)"
                    sheets.log_trade_exit(order, dict(trade), label, underlying_price)

                    # Single-leg position (qty < 2): exit1 sold the whole thing, so
                    # there is no second leg — mark it complete so it stops being
                    # monitored (and we never send a 0-qty exit2 order).
                    if (trade.get('exit2_qty') or 0) <= 0:
                        db.mark_exit2_filled(trade['id'], now)

                    # Opposite-side volume validation — may trigger early exit2
                    exit2_qty = trade.get('exit2_qty') or 0
                    if exit2_qty > 0 and option_quotes:
                        opp_valid = detector.check_opposite_side(
                            symbol, sig_type, option_quotes, target,
                        )
                        if opp_valid:
                            logger.info(
                                "OppSide validated at %s %.2f — early exit2  %s  qty=%d",
                                r1_label, target, occ, exit2_qty,
                            )
                            order2 = alpaca.close_position_qty(occ, exit2_qty)
                            if order2:
                                db.mark_exit2_filled(trade['id'], now)
                                sheets.log_trade_exit(
                                    order2, dict(trade),
                                    f"Exit 2/2 @ {r2_label} (opp-side early)",
                                    underlying_price,
                                )
                            if config.FLIP_ENABLED:
                                logger.info(
                                    "FLIP_ENABLED: opp-side confirmed at %s %.2f"
                                    " — flip not implemented",
                                    r1_label, target,
                                )
                        else:
                            logger.info(
                                "OppSide not active at %s %.2f — holding %s  qty=%d  for %s",
                                r1_label, target, occ, exit2_qty, r2_label,
                            )

        # ── Exit 2 — after exit 1 filled; price target OR opposite-side vol ──
        # (exit2_qty > 0 guard: a single-leg qty<2 trade has no second leg.)
        elif (trade['exit1_filled'] and not trade['exit2_filled']
              and trade.get('exit2_underlying') and (trade.get('exit2_qty') or 0) > 0):
            target2  = float(trade['exit2_underlying'])
            target1  = float(trade['exit1_underlying']) if trade.get('exit1_underlying') else 0.0
            r2_label = 'R2' if sig_type == 'BULLISH' else 'S2'

            level_hit = (
                (sig_type == 'BULLISH' and underlying_price >= target2) or
                (sig_type == 'BEARISH' and underlying_price <= target2)
            )

            # ── Chandelier trail on the runner (trail the runner) ──────────────
            # When enabled, the chandelier stop on the underlying REPLACES the fixed
            # Exit2 level once its ATR is ready — the runner rides the trend and stops
            # out on a reversal. Until the ATR is ready (need ~ATR_PERIOD+1 bars since
            # entry) the fixed level target still applies as a fallback.
            chand = None
            if config.CHANDELIER_EXIT_ENABLED and bars and trade.get('created_at'):
                since = [b for b in bars if b.get('bar_time')
                         and b['bar_time'] >= trade['created_at']] or bars
                chand = chandelier.evaluate(
                    sig_type, underlying_price, since,
                    period=config.CHANDELIER_ATR_PERIOD, mult=config.CHANDELIER_ATR_MULT)
            if chand and chand['ready']:
                price_hit  = bool(chand['exit'])
                exit_kind  = 'chandelier'
                exit_label = f"trail ${chand['stop']:.2f}"
                if not price_hit:
                    logger.debug("Chandelier hold %s runner  spot=%.2f  stop=%.2f  atr=%.3f",
                                 occ, underlying_price, chand['stop'], chand['atr'])
            else:
                price_hit  = level_hit
                exit_kind  = 'price'
                exit_label = r2_label

            # Still watching opposite side at exit1 level each bar
            opp_valid = False
            if option_quotes and target1:
                opp_valid = detector.check_opposite_side(
                    symbol, sig_type, option_quotes, target1,
                )

            if price_hit or opp_valid:
                trigger_reason = exit_kind if price_hit else 'opp-side'
                stop_disp = (chand['stop'] if exit_kind == 'chandelier' and chand else target2)
                logger.info(
                    "Exit2 triggered (%s)  %s  spot=%.2f  level=%.2f  qty=%d",
                    trigger_reason, occ, underlying_price, stop_disp, trade['exit2_qty'],
                )
                order = alpaca.close_position_qty(occ, trade['exit2_qty'])
                if order:
                    db.mark_exit2_filled(trade['id'], now)
                    suffix = ' (opp-side early)' if opp_valid and not price_hit else ''
                    label  = f"Exit 2/2 @ {exit_label}{suffix}"
                    sheets.log_trade_exit(order, dict(trade), label, underlying_price)
                    if opp_valid and config.FLIP_ENABLED:
                        logger.info(
                            "FLIP_ENABLED: opp-side confirmed at %s — flip not implemented",
                            r2_label,
                        )


def _eod_should_hold(trade: dict, alpaca: AlpacaClient) -> bool:
    """
    Decide whether a next-day-expiry ('Wednesday') position is carried overnight
    instead of closed at EOD.

    Rule: take profit, hold strong losers. A position in profit (or flat) is
    banked even if it never hit its R/S target. A position at a LOSS is held for
    another day ONLY if the originating signal was strong — confidence HIGH AND
    strong_cluster. Weak losers are cut. ('No reversal' is implicit: a trade that
    reversed is already closed, so anything still open here never reversed.)

    Returns False (→ close) when P&L is unknown, so we never hold on bad data.
    """
    occ = trade['occ_symbol']
    pl  = alpaca.position_unrealized_pl(occ)
    if pl is None or pl >= 0:
        return False                                    # profit/flat → bank it; unknown → close
    meta = db.get_signal_strength(trade.get('signal_id'))
    return meta.get('confidence') == 'HIGH' and meta.get('strong_cluster') is True


def eod_liquidate(alpaca: AlpacaClient, now: datetime, sheets: SheetsLogger) -> None:
    """
    Close positions at 14:55 CST.

    0DTE (expiry == today or None) is always closed — it expires today. Next-day+
    ('Wednesday') expiry positions follow the take-profit / hold-strong-losers
    rule (see _eod_should_hold): banked if in profit, held overnight if a strong
    loser, cut if a weak loser. When EOD_CLOSE_NEXT_DAY is off, every next-day
    position is held overnight regardless (legacy behavior).
    """
    today = today_cst()
    logger.info("EOD liquidation starting at %s", now.strftime('%H:%M CST'))

    open_trades = db.get_open_trades()
    closed = skipped = 0

    for trade in open_trades:
        expiry = trade.get('expiry')
        # expiry stored as date; if None treat as 0DTE (legacy rows)
        is_next_day = expiry is not None and expiry > today
        if is_next_day:
            if not config.EOD_CLOSE_NEXT_DAY:
                logger.info(
                    "EOD: skipping %s — expiry %s is next-day+ (EOD_CLOSE_NEXT_DAY off)",
                    trade['occ_symbol'], expiry,
                )
                skipped += 1
                continue
            # Wednesday-expiry: bank profits, hold strong losers another day.
            if _eod_should_hold(trade, alpaca):
                logger.info(
                    "EOD: HOLDING %s overnight — losing but strong (HIGH + strong_cluster); "
                    "expiry %s still has life",
                    trade['occ_symbol'], expiry,
                )
                skipped += 1
                continue

        # Close the remaining qty: 0DTE, a next-day winner (bank it), or a weak loser (cut it).
        remaining_qty = 0
        if not trade['exit1_filled']:
            remaining_qty = trade['qty']
        elif not trade['exit2_filled']:
            remaining_qty = trade['exit2_qty'] or 0

        if remaining_qty > 0:
            order = alpaca.close_position_qty(trade['occ_symbol'], remaining_qty)
        else:
            order = None

        db.mark_trade_eod_closed(trade['id'], now)
        eod_order = order or {'id': 'eod', 'qty': remaining_qty}
        sheets.log_trade_exit(eod_order, dict(trade), 'EOD Close', 0.0)
        closed += 1

    logger.info(
        "EOD liquidation complete — %d closed, %d next-day position(s) left open",
        closed, skipped,
    )


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

    # Databento is optional — Schwab handles all bars and option quotes.
    # Missing API key or login failure is non-fatal; positioning monitor is skipped.
    dbc: Optional[DatabentoClient] = None
    try:
        dbc = DatabentoClient()
        dbc.login(interactive=True)
    except Exception:
        logger.warning(
            "Databento unavailable (no API key or login failed) — "
            "positioning monitor disabled; Schwab handles all intraday data.",
            exc_info=True,
        )

    if args.login:
        logger.info("--login flag: session cached. Exiting.")
        return

    # Single-instance guard — a second copy would double every alert and the
    # Sheets write volume. Exit immediately if another instance is already running.
    if not single_instance.acquire(config.LOCK_FILE):
        logger.error(
            "Another Jakevolume instance already holds %s — exiting to avoid "
            "duplicate alerts. (Stop the other copy or the Task Scheduler job.)",
            config.LOCK_FILE,
        )
        sys.exit(1)

    # Databento Live is used only for the positioning monitor.
    try:
        if dbc:
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

    # Alpaca market data: the intraday signal data source (SIP stock feed + OPRA
    # options, incl. option price-history Schwab lacks). The morning OI-level
    # snapshot stays on Schwab because Alpaca exposes no live open interest.
    adata: Optional[AlpacaDataClient] = None
    if config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY:
        try:
            _ad = AlpacaDataClient()
            adata = _ad if _ad.verify() else None
        except Exception:
            logger.warning("Alpaca data init failed — falling back to Databento/none", exc_info=True)
            adata = None
    if adata is None:
        logger.warning("Alpaca data source unavailable — intraday will use Databento if configured")

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
    snap_done:   date | None = None   # guard: run snapshot only once per day
    eod_done:    date | None = None   # guard: run EOD liquidation only once per day
    review_done: date | None = None   # guard: run post-close signal review once per day

    logger.info(
        "Loop running. Snapshot @ %02d:%02d CST | Market hours 08:30–15:00 CST",
        config.SNAPSHOT_HOUR, config.SNAPSHOT_MINUTE,
    )

    while True:
        t0  = time.monotonic()
        now = now_cst()

        try:
            # Morning snapshot — once per trading day. Fires at the 08:20 window,
            # OR as a catch-up if the process started/restarted after the window
            # (watchdog crash-restart) so the day still gets levels + a briefing.
            if snap_done != now.date() and (is_snapshot_window(now) or is_past_snapshot(now)):
                if schwab:
                    # A prior process this session may have already snapshotted
                    # today (then crashed). Re-running would re-pull every chain
                    # and re-send the briefing, so skip if levels already exist.
                    if is_past_snapshot(now) and db.get_today_levels(config.SYMBOLS[0], now.date()):
                        logger.info(
                            "Snapshot already present for %s — skipping catch-up run",
                            now.date(),
                        )
                    else:
                        morning_snapshot(schwab, sheets, adata)
                else:
                    logger.warning("Schwab not available — morning snapshot skipped")
                snap_done = now.date()

            # Intraday signal scan — every minute during market hours
            if is_market_open(now):
                intraday_check(dbc, detector, monitor, sheets, data_src=adata, alpaca=alpaca)

            # EOD liquidation — 14:55 CST, once per day
            if alpaca and config.ALPACA_ENABLED and is_eod_window(now) and eod_done != now.date():
                eod_liquidate(alpaca, now, sheets)
                eod_done = now.date()

            # Daily signal review + nightly Claude pipeline — once per day after close.
            # analyze_daily_signals must complete first; the pipeline reads its output.
            if review_done != now.date() and is_post_close(now):
                try:
                    analyze_daily_signals(now.date(), data_src=adata, sheets=sheets)
                except Exception:
                    logger.warning("Daily signal review failed", exc_info=True)
                try:
                    from gate_report import build_gate_report
                    logger.info("Production volume-gate report:\n%s",
                                build_gate_report(now.date()))
                except Exception:
                    logger.warning("Gate report failed", exc_info=True)
                try:
                    run_nightly_pipeline(now.date())
                except Exception:
                    logger.warning("Nightly research pipeline failed", exc_info=True)
                review_done = now.date()

        except Exception:
            logger.exception("Unhandled error in main loop")

        # Sleep for the remainder of the 60-second interval
        elapsed   = time.monotonic() - t0
        sleep_for = max(0.0, config.POLL_INTERVAL_SECONDS - elapsed)
        logger.debug("Sleeping %.1fs until next poll", sleep_for)
        time.sleep(sleep_for)


if __name__ == '__main__':
    main()

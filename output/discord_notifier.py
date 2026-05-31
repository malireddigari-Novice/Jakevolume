"""
Discord notification layer — signal alerts and morning briefing.

Uses Discord incoming webhooks (no bot token required).
Configure in .env:
  DISCORD_WEBHOOK_URL          — receives intraday signal alerts
  DISCORD_MORNING_WEBHOOK_URL  — receives the 8:20 AM briefing
                                 (falls back to DISCORD_WEBHOOK_URL if not set)

Both can point to the same channel or different ones.
"""
import logging
from datetime import datetime
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_GREEN  = 0x00C851   # BULLISH signals
_RED    = 0xFF4444   # BEARISH signals
_BLUE   = 0x4A90D9   # morning briefing

_CONVICTION_EMOJI = {
    'WITH_BIAS':    '✅',
    'NEUTRAL':      '⚪',
    'AGAINST_BIAS': '⚠️',
}


# ── Low-level post ────────────────────────────────────────────────────────────

def _post(url: str, payload: dict) -> None:
    """POST to a Discord webhook. Logs on failure, never raises."""
    if not url:
        return
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            logger.warning(
                "Discord webhook HTTP %d: %s", resp.status_code, resp.text[:300]
            )
    except Exception:
        logger.warning("Discord webhook request failed", exc_info=True)


# ── Signal alert ──────────────────────────────────────────────────────────────

def _fmt_level(price: Optional[float]) -> str:
    """Format a price level: drop .0 for whole numbers, keep .5 for halves."""
    if price is None:
        return 'n/a'
    if price == int(price):
        return str(int(price))
    return f"{price:.1f}" if round(price % 1, 2) in (0.5, 0.25, 0.75) else f"{price:.2f}"


def _fmt_stop(entry: Optional[float]) -> str:
    """50% of entry price, expressed as cents or dollars."""
    if not entry:
        return 'n/a'
    stop = round(entry * 0.5, 2)
    if stop < 1.0:
        return f"{int(round(stop * 100))} cents"
    return f"${stop:.2f}"


def send_signal(sig: dict) -> None:
    """
    Send a fired signal as a compact two-line Discord embed.

    Line 1:  📈 TSLA 440C  2.15  5/29 @10:30
    Line 2:  Spot 440.29  |  Exit 1/2 @ R1 442.5  |  Exit 1/2 @ R2 445  |  Stoploss 80 cents
    """
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return

    signal_type = sig.get('signal_type', '')
    symbol      = sig.get('symbol', '')
    colour      = _GREEN if signal_type == 'BULLISH' else _RED
    arrow       = '📈' if signal_type == 'BULLISH' else '📉'

    # ── Line 1: symbol  strike+side  entry_price  expiry @time ───────────────
    price_to_enter = sig.get('price_to_enter')
    enter_str  = f"{price_to_enter:.2f}" if price_to_enter else 'n/a'

    strike     = sig.get('level_price')
    opt_type   = sig.get('option_type', '')
    side_char  = 'C' if opt_type == 'CALL' else 'P' if opt_type == 'PUT' else ''
    strike_str = f"{_fmt_level(strike)}{side_char}" if strike else ''

    expiry   = sig.get('expiry')
    expiry_s = f"{expiry.month}/{expiry.day}" if expiry else ''

    sig_time = sig.get('signal_time')
    if isinstance(sig_time, datetime):
        h = sig_time.hour % 12 or 12
        time_str = f"{h}:{sig_time.minute:02d}"
    else:
        time_str = ''

    line1 = f"{arrow} **{symbol} {strike_str}  {enter_str}  {expiry_s} @{time_str}**"

    # ── Line 2: exits and stoploss ─────────────────────────────────────────────
    exit1 = sig.get('exit1_price')
    exit2 = sig.get('exit2_price')
    lbl1, lbl2 = ('R1', 'R2') if signal_type == 'BULLISH' else ('S1', 'S2')

    spot = sig.get('trigger_price')

    parts: list[str] = []
    if spot is not None:
        parts.append(f"Spot {spot:.2f}")
    if exit1 is not None:
        parts.append(f"Exit 1/2 @ {lbl1} {_fmt_level(exit1)}")
    if exit2 is not None:
        parts.append(f"Exit 1/2 @ {lbl2} {_fmt_level(exit2)}")
    parts.append(f"Stoploss {_fmt_stop(price_to_enter)}")
    line2 = '  |  '.join(parts)

    # ── Line 3: volume detail ──────────────────────────────────────────────────
    atm_vol   = sig.get('atm_vol_1m')
    atm_ratio = sig.get('atm_spike_ratio')
    itm_vol   = sig.get('itm_vol_1m')
    itm_ratio = sig.get('itm_spike_ratio')

    vol_parts: list[str] = []
    if atm_vol is not None:
        ratio_s = f" x{atm_ratio:.1f}" if atm_ratio else ''
        vol_parts.append(f"ATM Vol {atm_vol:,}{ratio_s}")
    if itm_vol is not None:
        ratio_s = f" x{itm_ratio:.1f}" if itm_ratio else ''
        vol_parts.append(f"ITM Vol {itm_vol:,}{ratio_s}")
    line3 = '  |  '.join(vol_parts)

    # ── Assemble and send ──────────────────────────────────────────────────────
    prefix = "[SAMPLE] " if config.SAMPLE_MODE else ""
    ts = sig_time.isoformat() if isinstance(sig_time, datetime) else None

    body = f"{prefix}{line1}\n\n{line2}"
    if line3:
        body += f"\n\n{line3}"

    payload = {
        "embeds": [{
            "description": body,
            "color":  colour,
            "footer": {"text": "Jakevolume 0DTE" + (" — SAMPLE ONLY" if config.SAMPLE_MODE else "")},
            **({"timestamp": ts} if ts else {}),
        }]
    }
    _post(url, payload)
    logger.info("Discord: signal sent  %s %s  enter=%s", symbol, signal_type, enter_str)


# ── Morning briefing ──────────────────────────────────────────────────────────

def send_morning_briefing(results: list, now: datetime) -> None:
    """
    Send the 8:20 AM morning briefing as a single Discord message.

    `results` is the list built in run_morning_snapshot.py / morning_snapshot():
    each item has keys: symbol, prev_close, pm_price, expiry, supports,
    resistances, sentiment.
    """
    url = config.DISCORD_MORNING_WEBHOOK_URL or config.DISCORD_WEBHOOK_URL
    if not url:
        return

    header = (
        f"{'SYM':<6}  {'Prev':>8}  {'Bias':<18}  "
        f"{'P/C':>5}  {'S1':>7}  {'S2':>7}  {'S3':>7}  "
        f"{'R1':>7}  {'R2':>7}  {'R3':>7}  Expiry"
    )
    divider = "-" * len(header)

    rows = [header, divider]
    for r in results:
        s   = r['sentiment']
        sup = r['supports']
        res = r['resistances']

        s1 = f"{sup[0]['strike']:.1f}" if len(sup) > 0 else '  -  '
        s2 = f"{sup[1]['strike']:.1f}" if len(sup) > 1 else '  -  '
        s3 = f"{sup[2]['strike']:.1f}" if len(sup) > 2 else '  -  '
        r1 = f"{res[0]['strike']:.1f}" if len(res) > 0 else '  -  '
        r2 = f"{res[1]['strike']:.1f}" if len(res) > 1 else '  -  '
        r3 = f"{res[2]['strike']:.1f}" if len(res) > 2 else '  -  '

        rows.append(
            f"{r['symbol']:<6}  "
            f"{r['prev_close']:>8.2f}  "
            f"{s['bias']:<18}  "
            f"{s['pc_ratio']:>5.3f}  "
            f"{s1:>7}  {s2:>7}  {s3:>7}  "
            f"{r1:>7}  {r2:>7}  {r3:>7}  "
            f"{r['expiry']}"
        )

    table = "\n".join(rows)
    prefix = "**[SAMPLE]** " if config.SAMPLE_MODE else ""
    title = f"{prefix}**JAKEVOLUME MORNING BRIEFING — {now.strftime('%Y-%m-%d %H:%M CST')}**"

    # Discord has a 2000-char message limit; the table fits comfortably in a code block.
    content = f"{title}\n```\n{table}\n```"

    _post(url, {"content": content})
    logger.info("Discord: morning briefing sent (%d symbols)", len(results))


# ── Trade execution alert ─────────────────────────────────────────────────────

def send_trade_alert(order: dict, sig: dict, qty: int, spend: float) -> None:
    """
    Send a blue embed to Discord when Alpaca places an order.

    order   : Alpaca order response dict (includes 'id', 'symbol')
    sig     : the originating signal dict
    qty     : number of contracts ordered
    spend   : dollars allocated (qty * limit_price * 100)
    """
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return

    mode    = "PAPER" if config.ALPACA_PAPER else "LIVE"
    prefix  = "[SAMPLE] " if config.SAMPLE_MODE else ""
    occ     = order.get('symbol', sig.get('symbol', ''))
    order_id = str(order.get('id', ''))[:8]
    limit   = order.get('limit_price') or sig.get('price_to_enter') or 0

    fields = [
        {"name": "Contract",       "value": occ,                              "inline": True},
        {"name": "Qty",            "value": f"{qty} contract{'s' if qty!=1 else ''}",  "inline": True},
        {"name": "Limit Price",    "value": f"${float(limit):.2f}",           "inline": True},
        {"name": "Capital Used",   "value": f"${spend:,.2f}",                 "inline": True},
        {"name": "Signal",         "value": sig.get('signal_type', ''),       "inline": True},
        {"name": "Order ID",       "value": order_id,                         "inline": True},
    ]

    payload = {
        "embeds": [{
            "title":  f"{prefix}🔔 ORDER PLACED [{mode}] — {occ}",
            "color":  0x4A90D9,
            "fields": fields,
            "footer": {"text": f"Jakevolume 0DTE — Alpaca {mode}" +
                               (" | SAMPLE ONLY" if config.SAMPLE_MODE else "")},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }
    _post(url, payload)
    logger.info("Discord: trade alert sent  %s  qty=%d  spend=$%.2f", occ, qty, spend)


# ── Exit alert ────────────────────────────────────────────────────────────────

def send_exit_alert(
    order:            dict,
    trade:            dict,
    exit_label:       str,   # 'Exit 1/2 @ R1', 'Exit 2/2 @ R2', 'EOD Close', etc.
    underlying_price: float,
) -> None:
    """Send a sell confirmation embed to Discord."""
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return

    mode   = "PAPER" if config.ALPACA_PAPER else "LIVE"
    prefix = "[SAMPLE] " if config.SAMPLE_MODE else ""
    occ    = trade.get('occ_symbol', order.get('symbol', ''))
    qty    = order.get('qty', trade.get('exit1_qty', '?'))
    order_id = str(order.get('id', ''))[:8]

    fields = [
        {"name": "Contract",       "value": occ,                    "inline": True},
        {"name": "Qty Sold",       "value": str(qty),               "inline": True},
        {"name": "Spot at Exit",   "value": f"${underlying_price:.2f}", "inline": True},
        {"name": "Order ID",       "value": order_id,               "inline": True},
        {"name": "Signal",         "value": trade.get('signal_type', ''), "inline": True},
    ]

    payload = {
        "embeds": [{
            "title":  f"{prefix}✅ {exit_label} [{mode}] — {occ}",
            "color":  0xFFAA00,
            "fields": fields,
            "footer": {"text": f"Jakevolume 0DTE — Alpaca {mode}" +
                               (" | SAMPLE ONLY" if config.SAMPLE_MODE else "")},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }
    _post(url, payload)
    logger.info("Discord: exit alert sent  %s  %s", occ, exit_label)

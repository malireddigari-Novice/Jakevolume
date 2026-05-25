"""
Discord notification layer — signal alerts and morning briefing.

Uses Discord incoming webhooks (no bot token required).
Configure in .env:
  DISCORD_WEBHOOK_URL          — receives intraday signal alerts
  DISCORD_MORNING_WEBHOOK_URL  — receives the 8:10 AM briefing
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

def send_signal(sig: dict) -> None:
    """
    Send a fired signal as a colour-coded Discord embed.

    Green  = BULLISH (call at support)
    Red    = BEARISH (put at resistance)
    """
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return

    signal_type = sig.get('signal_type', '')
    symbol      = sig.get('symbol', '')
    colour      = _GREEN if signal_type == 'BULLISH' else _RED
    arrow       = '📈' if signal_type == 'BULLISH' else '📉'

    opt_char = 'C' if sig.get('option_type') == 'CALL' else 'P'
    expiry   = sig.get('expiry')
    expiry_s = expiry.strftime('%m/%d') if expiry else ''
    strike   = sig.get('level_price', '')
    contract = f"{symbol} {strike}{opt_char} {expiry_s}".strip()

    enter = f"${sig['price_to_enter']:.2f}" if sig.get('price_to_enter') else 'n/a'
    exit_ = f"${sig['price_to_exit']:.2f}"  if sig.get('price_to_exit')  else 'n/a'
    spot  = f"${sig.get('trigger_price', 0):.2f}"

    room_pct   = sig.get('room_pct')
    room_str   = f"{room_pct * 100:.2f}%" if room_pct else '∞'
    room_score = sig.get('room_score', '')

    pc_ratio     = sig.get('pc_ratio')
    pc_conviction = sig.get('pc_conviction', 'NEUTRAL')
    pc_emoji     = _CONVICTION_EMOJI.get(pc_conviction, '⚪')
    pc_str = (
        f"{pc_emoji} {pc_conviction}  P/C={pc_ratio:.3f}"
        if pc_ratio is not None else f"{pc_emoji} {pc_conviction}"
    )

    spread = sig.get('spread_pct')
    spread_str = f"{spread * 100:.1f}%" if spread is not None else 'n/a'

    atm_1m = sig.get('atm_vol_1m', 0)
    itm_1m = sig.get('itm_vol_1m', 0)
    atm_3m = sig.get('atm_vol_3m', 0)
    itm_3m = sig.get('itm_vol_3m', 0)

    fields = [
        {"name": "Spot Price",    "value": spot,                                     "inline": True},
        {"name": "Enter (Ask)",   "value": enter,                                    "inline": True},
        {"name": "Exit Target",   "value": exit_,                                    "inline": True},
        {"name": "Flow Shape",    "value": sig.get('flow_shape', 'n/a'),             "inline": True},
        {"name": "Prox Score",    "value": str(sig.get('prox_score', '')),           "inline": True},
        {"name": "Strong",        "value": 'YES' if sig.get('strong_cluster') else 'NO', "inline": True},
        {"name": "ATM Vol",       "value": f"1m={atm_1m}  3m={atm_3m}",             "inline": True},
        {"name": "ITM Vol",       "value": f"1m={itm_1m}  3m={itm_3m}",             "inline": True},
        {"name": "Spread",        "value": spread_str,                               "inline": True},
        {"name": "Target Room",   "value": f"{room_str}  [score {room_score}]",      "inline": True},
        {"name": "PC Conviction", "value": pc_str,                                   "inline": True},
    ]

    sig_time = sig.get('signal_time')
    ts = sig_time.isoformat() if isinstance(sig_time, datetime) else None

    prefix = "[SAMPLE] " if config.SAMPLE_MODE else ""
    payload = {
        "embeds": [{
            "title":  f"{prefix}{arrow} {signal_type} — {contract}",
            "color":  colour,
            "fields": fields,
            "footer": {"text": "Jakevolume 0DTE" + (" — SAMPLE ONLY" if config.SAMPLE_MODE else "")},
            **({"timestamp": ts} if ts else {}),
        }]
    }
    _post(url, payload)
    logger.info("Discord: signal sent  %s %s  enter=%s", symbol, signal_type, enter)


# ── Morning briefing ──────────────────────────────────────────────────────────

def send_morning_briefing(results: list, now: datetime) -> None:
    """
    Send the 8:10 AM morning briefing as a single Discord message.

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

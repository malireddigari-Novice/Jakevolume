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
    Send a Simplified V1 entry alert (§20) — a single compact card:

        AAPL 315P 6/9 @ 1.40
        Spot: 315.20
        Level: R1 315
        Expiry: 6/9
        Volume: 497
        Ratio: 12.4x
        ContractLowDistance: 1.18

    No volume-shape label, spread, target room, or exit/stop lines (§1).
    """
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return

    signal_type = sig.get('signal_type', '')
    symbol      = sig.get('symbol', '')
    colour      = _GREEN if signal_type == 'BULLISH' else _RED
    arrow       = '📈' if signal_type == 'BULLISH' else '📉'

    price_to_enter = sig.get('price_to_enter')
    enter_str  = f"{price_to_enter:.2f}" if price_to_enter else 'n/a'

    opt_type   = sig.get('option_type', '')
    side_char  = 'C' if opt_type == 'CALL' else 'P' if opt_type == 'PUT' else ''
    # The headline strike is the contract we'd actually buy (traded_strike). In
    # next-day mode this is the OTM target strike, which differs from the detection
    # level (level_price) — the price_to_enter belongs to THIS strike, so the card
    # must label it as such (a 420-level BEARISH signal trading the 410 put shows
    # "410P @ 2.57", with "Level: R1 420" below).
    level_strike = sig.get('level_price')
    trade_strike = sig.get('traded_strike') or level_strike
    strike_str   = f"{_fmt_level(trade_strike)}{side_char}" if trade_strike else ''

    expiry    = sig.get('expiry')
    expiry_s  = f"{expiry.month}/{expiry.day}" if expiry else ''

    spot      = sig.get('trigger_price')
    label     = sig.get('level_label', '')
    # Trigger volume/ratio from the VolumeStickoutScore (the actual trigger),
    # falling back to the legacy 1-min fields if absent.
    tv_type   = sig.get('trigger_volume_type')
    trig_vol  = sig.get('trigger_volume')
    trig_ratio = sig.get('trigger_ratio')
    if trig_vol is None:
        trig_vol, trig_ratio, tv_type = sig.get('atm_vol_1m'), sig.get('atm_spike_ratio'), 'SINGLE_BAR'
    vol_label = "Volume 5m" if tv_type == 'FIVE_BAR_WINDOW' else "Volume"
    ratio_label = "Ratio 5m" if tv_type == 'FIVE_BAR_WINDOW' else "Ratio"
    low_dist  = sig.get('low_dist')

    sig_time = sig.get('signal_time')
    ts = sig_time.isoformat() if isinstance(sig_time, datetime) else None

    head = f"{symbol} {strike_str}" + (f" {expiry_s}" if expiry_s else "") + f" @ {enter_str}"
    lines = [f"{arrow} **{head}**"]
    if spot is not None:
        lines.append(f"Spot: {spot:.2f}")
    lines.append(f"Level: {label} {_fmt_level(level_strike)}".rstrip())
    if expiry_s:
        lines.append(f"Expiry: {expiry_s}")
    if trig_vol is not None:
        lines.append(f"{vol_label}: {int(trig_vol):,}")
    if trig_ratio:
        lines.append(f"{ratio_label}: {trig_ratio:.1f}x")
    if low_dist is not None:
        lines.append(f"ContractLowDistance: {low_dist:.2f}")

    # Shifted exit targets (skip the too-close nearest level).
    exit1 = sig.get('exit1_price')
    exit2 = sig.get('exit2_price')
    if exit1 is not None:
        lines.append(f"Exit 1/2 @ {_fmt_level(exit1)}")
    if exit2 is not None:
        lines.append(f"Exit rest @ {_fmt_level(exit2)}")

    prefix = "[SAMPLE] " if config.SAMPLE_MODE else ""
    payload = {
        "embeds": [{
            "description": prefix + "\n".join(lines),
            "color":  colour,
            "footer": {"text": "Jakevolume V1" + (" — SAMPLE ONLY" if config.SAMPLE_MODE else "")},
            **({"timestamp": ts} if ts else {}),
        }]
    }
    _post(url, payload)
    logger.info("Discord: signal sent  %s %s  enter=%s", symbol, signal_type, enter_str)


# ── Morning briefing ──────────────────────────────────────────────────────────

# Bias → embed border color. A quick visual cue only; the bias is always printed
# as text in the title too, so readability never depends on color.
_BIAS_COLORS = {
    'STRONGLY BULLISH': 0x1A7F37,   # green
    'BULLISH':          0x2DA44E,   # lighter green
    'NEUTRAL':          0x6E7781,   # gray
    'BEARISH':          0xBC6B00,   # muted orange
    'STRONGLY BEARISH': 0xCF222E,   # red
}


def _bias_color(bias: str) -> int:
    """Embed border color for a sentiment bias; gray (NEUTRAL) by default."""
    return _BIAS_COLORS.get((bias or '').upper().strip(), 0x6E7781)


def _fmt_expiry(expiry) -> str:
    """Format an expiry as 'Jun 17'; tolerate date/datetime, str, or None."""
    if expiry is None:
        return 'n/a'
    if hasattr(expiry, 'strftime'):
        return expiry.strftime('%b %d')
    return str(expiry)


def _level_lines(levels: list, prefix: str) -> str:
    """
    Stacked rank-labelled lines ('S1: $295.00') in OI-rank order — rank 1 first.

    Levels are NOT re-sorted by price: the ranks reflect OI strength, so the
    values may not be numerically ordered. That is expected; preserve the rank.
    """
    ranked = sorted(levels, key=lambda lv: lv.get('rank', 99))
    if not ranked:
        return '—'
    return "\n".join(
        f"{prefix}{i}: ${lv['strike']:.2f}" for i, lv in enumerate(ranked[:3], start=1)
    )


def _build_symbol_embed(r: dict, footer: dict) -> dict:
    """One mobile-first embed per symbol: bias-colored border, stacked S/R fields."""
    s    = r['sentiment']
    bias = s.get('bias', 'NEUTRAL')
    prev = r.get('prev_close')
    pc   = s.get('pc_ratio')
    prev_str = f"${prev:.2f}" if prev is not None else 'n/a'
    pc_str   = f"{pc:.3f}" if pc is not None else 'n/a'
    return {
        'title': f"{r['symbol']} — {bias}",
        'color': _bias_color(bias),
        'description': (
            f"**Previous Close:** {prev_str}\n"
            f"**Expiry:** {_fmt_expiry(r.get('expiry'))}\n"
            f"**Put/Call OI:** {pc_str}"
        ),
        'fields': [
            {'name': 'Support Levels',
             'value': _level_lines(r.get('supports', []), 'S'),
             'inline': False},      # stacked (not side-by-side) for mobile width
            {'name': 'Resistance Levels',
             'value': _level_lines(r.get('resistances', []), 'R'),
             'inline': False},
        ],
        'footer': footer,
    }


def send_morning_briefing(results: list, now: datetime, oi_buildup: list | None = None) -> None:
    """
    Send the 8:20 AM morning briefing to Discord as one mobile-first embed per
    symbol — bias-colored border, stacked Support/Resistance fields with rank
    labels beside each value — instead of a wide monospaced table that wraps and
    detaches labels on mobile.

    `results` is the list built in run_morning_snapshot.py / morning_snapshot():
    each item has keys: symbol, prev_close, pm_price, expiry, supports,
    resistances, sentiment (which carries bias + pc_ratio).
    """
    url = config.DISCORD_MORNING_WEBHOOK_URL or config.DISCORD_WEBHOOK_URL
    if not url or not results:
        return

    prefix   = "**[SAMPLE]** " if config.SAMPLE_MODE else ""
    time_str = now.strftime('%I:%M %p').lstrip('0')        # "08:20 AM" → "8:20 AM"
    header   = (
        f"{prefix}**JAKEVOLUME MORNING BRIEFING**\n"
        f"{now.strftime('%B %d, %Y')} — {time_str} CST\n"
        f"Market universe: " + " · ".join(r['symbol'] for r in results)
    )
    footer = {'text': f"Jakevolume Morning Briefing · {time_str} CST"}

    embeds = []
    for r in results:
        try:
            embeds.append(_build_symbol_embed(r, footer))
        except Exception:
            logger.warning("Discord: failed to build briefing embed for %s",
                           r.get('symbol'), exc_info=True)

    # Optional overnight OI buildup as a compact trailing embed.
    if oi_buildup:
        bu_lines = []
        for b in oi_buildup:
            ch    = b.get('oi_change', 0) or 0
            pct   = b.get('oi_change_pct')
            pcts  = f" ({pct:+.0%})" if pct is not None else ""
            dist  = b.get('distance_pct')
            dists = f" · dist {dist:+.1%}" if dist is not None else ""
            bu_lines.append(
                f"{b['symbol']} {b['option_type']} ${b['strike']:.0f} · +{ch:,} OI{pcts}{dists}"
            )
        embeds.append({
            'title': 'OI Buildup (overnight)',
            'color': 0x6E7781,
            'description': "\n".join(bu_lines) or '—',
            'footer': footer,
        })

    # Discord allows up to 10 embeds per webhook message; chunk to stay safe
    # (MAG-7 = 7 symbols, so this is normally a single call). The header content
    # rides on the first message only.
    first = True
    for i in range(0, len(embeds), 10):
        payload = {'embeds': embeds[i:i + 10]}
        if first:
            payload['content'] = header
            first = False
        _post(url, payload)

    logger.info("Discord: morning briefing sent (%d symbols, embed format)", len(results))


def send_reversal_alert(rev: dict) -> None:
    """
    Flow-leadership reversal: the opposite side took control while the position's side
    faded. Posts the exit + the hypothetical opposite entry (V1 paper-tracks it).
    `rev` keys: symbol, from_side, to_side, spot, exit_occ, exit_price, hypo_occ,
    hypo_entry_price, opp_leadership, same_leadership, opp_burst, opp_share, flipped.
    """
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return
    flipped = rev.get('flipped')
    title = f"🔄 **FLOW REVERSAL — {rev['symbol']} {rev['from_side']} → {rev['to_side']}**"
    lines = [
        title,
        f"Opposite-side flow took control; exiting {rev['from_side']} side.",
        f"Spot: {rev.get('spot')}",
        f"Exited: {rev.get('exit_occ')} @ {rev.get('exit_price')}",
        f"Leadership: {rev['to_side']} {rev.get('opp_leadership')} vs "
        f"{rev['from_side']} {rev.get('same_leadership')}  "
        f"(opp burst {rev.get('opp_burst')}x / share {rev.get('opp_share')})",
        (f"➡️ Flipped to {rev.get('hypo_occ')} @ {rev.get('hypo_entry_price')}" if flipped
         else f"Hypothetical {rev['to_side']} entry (paper-tracked): "
              f"{rev.get('hypo_occ')} @ {rev.get('hypo_entry_price')}"),
    ]
    _post(url, {"content": "\n".join(str(x) for x in lines)})
    logger.info("Discord: reversal alert sent  %s %s->%s",
                rev['symbol'], rev['from_side'], rev['to_side'])


def send_daily_review(rows: list, analysis_date) -> None:
    """
    Post the daily post-close signal review: one line per signal with the realized
    peak (MFE), the current rule's P&L, and the suggested management + its outcome.
    `rows` are the dicts built by analysis.daily_review.analyze_daily_signals().
    """
    url = (config.DISCORD_REVIEW_WEBHOOK_URL or config.DISCORD_MORNING_WEBHOOK_URL
           or config.DISCORD_WEBHOOK_URL)
    if not url or not rows:
        return

    header = f"{'SYM':<6} {'Dir':<4} {'MFE%':>7} {'Rule%':>7} {'Sugg%':>7}  Suggested action"
    lines  = [header, "-" * len(header)]
    for r in sorted(rows, key=lambda x: (x.get('mfe_pct') or -999), reverse=True):
        mfe  = r.get('mfe_pct'); rule = r.get('rule_pnl_pct'); sug = r.get('suggested_pnl_pct')
        lines.append(
            f"{r.get('symbol',''):<6} {str(r.get('signal_type',''))[:4]:<4} "
            f"{(f'{mfe:+.0f}' if mfe is not None else '  -'):>7} "
            f"{(f'{rule:+.0f}' if rule is not None else '  -'):>7} "
            f"{(f'{sug:+.0f}' if sug is not None else '  -'):>7}  "
            f"{r.get('suggested_action','')}"
        )
    # Detail block: the full recommendation text per signal
    notes = [f"- {r.get('symbol','')}: {r.get('suggestion','')}" for r in rows if r.get('suggestion')]

    prefix = "**[SAMPLE]** " if config.SAMPLE_MODE else ""
    title  = f"{prefix}**JAKEVOLUME DAILY REVIEW — {analysis_date}**  ({len(rows)} signals)"
    table  = "\n".join(lines)
    body   = "\n".join(notes)
    content = f"{title}\n```\n{table}\n```\n{body}"
    if len(content) > 1900:               # Discord 2000-char limit — drop the notes if long
        content = f"{title}\n```\n{table}\n```"
    _post(url, {"content": content})
    logger.info("Discord: daily review sent (%d signals)", len(rows))


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

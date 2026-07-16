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
    # §13 — the production gate's trigger: prefer the completed 1-min bar, show the
    # observed partial when it differed, and the trigger shape (1-min vs multi-min).
    tv_type   = sig.get('trigger_volume_type')
    trig_vol  = sig.get('trigger_volume')
    trig_ratio = sig.get('trigger_ratio')
    if trig_vol is None:
        trig_vol, trig_ratio, tv_type = sig.get('atm_vol_1m'), sig.get('atm_spike_ratio'), 'SINGLE_BAR'
    trig_type_label = ("1-Minute Print" if tv_type == 'SINGLE_BAR'
                       else "Multi-Minute Window" if tv_type == 'MULTI_MIN_WINDOW'
                       else "Volume")
    observed   = sig.get('observed_vol')
    bar_status = sig.get('bar_status')
    peak1m     = sig.get('peak_1m')
    vol3m      = sig.get('vol3m_window') or sig.get('vol_3m_window')
    low_dist   = sig.get('low_dist')
    gold       = sig.get('gold_standard')

    sig_time = sig.get('signal_time')
    ts = sig_time.isoformat() if isinstance(sig_time, datetime) else None

    context = sig.get('signal_context')
    head = f"{symbol} {strike_str}" + (f" {expiry_s}" if expiry_s else "") + f" @ {enter_str}"
    lines = [f"{arrow} **{head}**" + ("  ⭐" if gold else "")]

    def _latency_line():
        """§17 flow-event → alert latency, one compact line; None when no profile."""
        lat = sig.get('latency') or {}
        total = lat.get('total_latency_secs')
        if total is None:
            return None
        bar_wait = lat.get('bar_wait_secs')
        seg = f" (bar {bar_wait:.0f}s)" if bar_wait is not None else ""
        return f"Latency: {total:.0f}s event→alert{seg}"

    def _positioning_line():
        """Layer-3 fresh-OI alignment for this signal; None when not evaluated."""
        al = sig.get('positioning_alignment')
        if not al or al in ('NONE', 'NEUTRAL'):
            return None
        icon = '🟢' if al == 'ALIGNED' else '🔴'
        d = sig.get('positioning_delta', 0)
        return f"Fresh-OI: {icon} {sig.get('positioning_note', al)} (conf {d:+d})"

    # Gold-mode classification line (only surfaced while the mode is active, so the
    # card is unchanged when GOLD_ONLY_PRODUCTION_MODE is off).
    if config.GOLD_ONLY_PRODUCTION_MODE and sig.get('gold_grade'):
        _vr = sig.get('value_region')
        lines.append(f"Gold: {sig.get('gold_subtype')} [{sig.get('gold_grade')}]"
                     + (f" · {_vr}" if _vr else ""))

    # ── §18 Chain-led emergent card ──
    if context == 'CHAIN_LED_EMERGENT_ENTRY':
        emergent_spot = sig.get('emergent_spot')
        chain_strikes = sig.get('chain_strikes') or []
        comb3m = sig.get('chain_combined_3m')
        notional = sig.get('premium_notional')
        side_word = 'CALL' if opt_type == 'CALL' else 'PUT'
        loc_label = 'Emergent Support' if opt_type == 'CALL' else 'Emergent Resistance'
        lines.append(f"Signal: CHAIN-LED {side_word}")
        if emergent_spot is not None:
            lines.append(f"{loc_label}: {emergent_spot:.2f}")
        if chain_strikes:
            lines.append("Chain: " + " + ".join(f"{_fmt_level(s)}{side_char}" for s in chain_strikes))
        if comb3m:
            lines.append(f"Combined 3m Vol: {int(comb3m):,}")
        if trig_vol is not None:
            lines.append(f"ATM Volume: {int(trig_vol):,}")
        if notional:
            lines.append(f"Premium Notional: ${int(notional):,}")
        if low_dist is not None:
            lines.append(f"Contract Low Distance: {low_dist:.2f}")
        t1, t2 = sig.get('exit1_price'), sig.get('exit2_price')
        if t1 is not None:
            lines.append(f"Targets: 1/2 @ {_fmt_level(t1)}" + (f"  Rest @ {_fmt_level(t2)}" if t2 else ""))
        _lat = _latency_line()
        if _lat:
            lines.append(_lat)
        _pos = _positioning_line()
        if _pos:
            lines.append(_pos)
        prefix = "[SAMPLE] " if config.SAMPLE_MODE else ""
        _post(url, {"embeds": [{"description": prefix + "\n".join(lines), "color": colour,
                    "footer": {"text": "Jakevolume V1 — CHAIN-LED"},
                    **({"timestamp": ts} if ts else {})}]})
        logger.info("Discord: CHAIN-LED %s signal sent  %s", side_word, symbol)
        return

    # ── Primary-level card ──
    if context == 'PRIMARY_LEVEL_COUNTERTREND_REVERSAL':
        side_word = 'PUT' if opt_type == 'PUT' else 'CALL'
        lines.append(f"Signal: PRIMARY LEVEL {side_word}")
        lines.append("Trigger: Countertrend Reversal Confirmed")
    else:
        lines.append("Signal: PRIMARY LEVEL")
    if spot is not None:
        lines.append(f"Spot: {spot:.2f}")
    lines.append(f"Level: {label} {_fmt_level(level_strike)}".rstrip())
    if expiry_s:
        lines.append(f"Expiry: {expiry_s}")
    if trig_vol is not None:
        lines.append(f"Trigger: {trig_type_label}")
        bar_tag = f" ({bar_status.lower()})" if bar_status else ""
        lines.append(f"Trigger Volume: {int(trig_vol):,}{bar_tag}")
        if observed is not None and int(observed) != int(trig_vol):
            lines.append(f"Observed at Alert: {int(observed):,}")
        if peak1m is not None and int(peak1m) != int(trig_vol):
            lines.append(f"Peak 1m Volume: {int(peak1m):,}")
        if vol3m:
            lines.append(f"3m Volume: {int(vol3m):,}")
    if trig_ratio:
        lines.append(f"Ratio: {trig_ratio:.1f}x")
    if low_dist is not None:
        lines.append(f"Contract Low Distance: {low_dist:.2f}")

    # Shifted exit targets (skip the too-close nearest level).
    exit1 = sig.get('exit1_price')
    exit2 = sig.get('exit2_price')
    if exit1 is not None:
        lines.append(f"Exit 1/2 @ {_fmt_level(exit1)}")
    if exit2 is not None:
        lines.append(f"Exit rest @ {_fmt_level(exit2)}")

    _lat = _latency_line()
    if _lat:
        lines.append(_lat)
    _pos = _positioning_line()
    if _pos:
        lines.append(_pos)

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
    Stacked rank-labelled lines ('R1: $277.50 — 12,400 OI') ordered by PROXIMITY
    to spot — rank 1 = nearest level, rank 3 = furthest. A trailing ' *' marks the
    strike holding the highest open interest on that side (the dominant OI wall).

    Display ordering only: the persisted `rank` field (set in compute_oi_levels)
    still reflects OI strength and drives the signal gates — it is left untouched.
    Resistance strikes sit at/above spot and supports at/below, so nearest-first is
    ascending strike for resistance ('R') and descending strike for support ('S').
    """
    if not levels:
        return '—'
    by_proximity = sorted(levels, key=lambda lv: lv['strike'], reverse=(prefix == 'S'))[:3]
    max_oi = max((lv.get('open_interest', 0) or 0) for lv in by_proximity)
    lines = []
    for i, lv in enumerate(by_proximity, start=1):
        oi   = lv.get('open_interest', 0) or 0
        star = ' *' if oi and oi == max_oi else ''
        lines.append(f"{prefix}{i}: ${lv['strike']:.2f} — {oi:,} OI{star}")
    return "\n".join(lines)


def _build_symbol_embed(r: dict, footer: dict) -> dict:
    """One mobile-first embed per symbol: bias-colored border, stacked S/R fields."""
    s    = r['sentiment']
    bias = s.get('bias', 'NEUTRAL')
    prev = r.get('prev_close')
    pc   = s.get('pc_ratio')
    prev_str = f"${prev:.2f}" if prev is not None else 'n/a'
    pc_str   = f"{pc:.3f}" if pc is not None else 'n/a'
    desc = (
        f"**Previous Close:** {prev_str}\n"
        f"**Expiry:** {_fmt_expiry(r.get('expiry'))}\n"
        f"**Put/Call OI:** {pc_str}"
    )
    # Relative strength vs QQQ (only when computed). Flags names moving independent
    # of the index: 🟢 relatively strong, 🔴 relatively weak, · in-line.
    rs_val = s.get('rs')
    if rs_val is not None:
        cls  = s.get('rs_class', 'IN_LINE')
        icon = '🟢' if cls == 'RELATIVELY_STRONG' else '🔴' if cls == 'RELATIVELY_WEAK' else '·'
        desc += f"\n**vs QQQ:** {icon} {rs_val:+.2f}% ({s.get('rs_tag', 'in-line')})"

    # ATM 0DTE window (ATM + 1-OTM per side): strike, premium, and OI.
    atm = s.get('atm_0dte')
    if atm:
        def _leg(d, suffix):
            if not d or d.get('strike') is None:
                return None
            px = d.get('mark')
            if px is None and d.get('bid') is not None and d.get('ask') is not None:
                px = (d['bid'] + d['ask']) / 2
            oi = d.get('open_interest')
            oi_s = f" · {int(oi):,} OI" if oi is not None else ""
            return f"{_fmt_level(d['strike'])}{suffix} " + (f"${px:.2f}" if px is not None else "n/a") + oi_s
        calls = [x for x in (_leg(c, 'C') for c in (atm.get('call') or [])) if x]
        puts  = [x for x in (_leg(p, 'P') for p in (atm.get('put') or [])) if x]
        if calls:
            desc += "\n**ATM 0DTE C:** " + "  ·  ".join(calls)
        if puts:
            desc += "\n**ATM 0DTE P:** " + "  ·  ".join(puts)

    # Overnight positioning (Fresh-OI heat-map): where institutions placed NEW risk since
    # the prior session. Context only — the battlefield, not a trade trigger.
    pos = s.get('positioning')
    if pos and pos.get('fresh_count'):
        side = pos.get('dominant_side')
        icon = '🟢' if side == 'CALL' else '🔴' if side == 'PUT' else '⚪'
        score = pos['bull_score'] if side == 'CALL' else pos['bear_score'] if side == 'PUT' else max(pos['bull_score'], pos['bear_score'])
        clus = (f" @ {_fmt_level(pos['cluster_low'])}-{_fmt_level(pos['cluster_high'])}"
                if pos.get('cluster_low') is not None else "")
        net = pos.get('net_notional') or 0
        net_s = f"${net/1e6:.1f}M" if net >= 1e6 else f"${net/1e3:.0f}k"
        desc += (f"\n**Fresh OI:** {icon} {side} {score:.1f}/10 · {pos.get('concentration','').replace('_',' ').title()} conc"
                 f" · {net_s} fresh{clus}")
    return {
        'title': f"{r['symbol']} — {bias}",
        'color': _bias_color(bias),
        'description': desc,
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


def _weekend_gap_line(g: dict) -> str:
    """One 'CALL $290 · Jul 03 · 18,400→41,200 (+22,800, +124%)' weekend-gap line."""
    ch   = g.get('oi_change', 0) or 0
    pct  = g.get('oi_change_pct')
    pcts = f", {pct:+.0%}" if pct is not None else ""
    sign = "+" if ch >= 0 else ""
    return (
        f"{g['option_type']} ${g['strike']:.0f} · {_fmt_expiry(g.get('expiry'))} · "
        f"{g.get('prev_open_interest', 0):,}→{g.get('open_interest', 0):,} "
        f"({sign}{ch:,}{pcts})"
    )


def _open_positions_embed(summary: dict | None, footer: dict) -> dict | None:
    """
    Embed for the morning open-position check. Green 'flat' confirmation when there
    is nothing open; a red carryover list otherwise, flag per position (EXPIRED /
    UNTRACKED / TRACKED). Returns None when the check was skipped (summary is None).
    """
    if summary is None:
        return None
    if summary.get('flat'):
        return {
            'title': '✅ Open Positions — none',
            'color': 0x1A7F37,
            'description': 'Flat into the session — no open option positions in Alpaca.',
            'footer': footer,
        }
    mark = {'EXPIRED': '⛔', 'UNTRACKED': '⚠️', 'TRACKED': '•'}
    lines = [
        f"{mark.get(p['flag'], '•')} {p['symbol']} {p['option_type']} ${p['strike']:.0f} · "
        f"{_fmt_expiry(p['expiry'])} · qty {p['qty']} · ${p['current_price']:.2f} · "
        f"uPL ${p['unrealized_pl']:+.0f} · {p['flag']}"
        for p in summary['positions']
    ]
    return {
        'title': '⚠️ Open Positions (carryover — review)',
        'color': 0xCF222E,
        'description': "\n".join(lines),
        'footer': footer,
    }


def _benchmark_embed(benchmarks: list, footer: dict) -> dict | None:
    """Compact SPY/QQQ context line (pre-market % change). None when unavailable."""
    if not benchmarks:
        return None
    def _fmt(b):
        p = b.get('pct')
        spot = b.get('pm_price')
        pstr = f"{p:+.2f}%" if p is not None else 'n/a'
        arrow = '▲' if (p or 0) > 0 else '▼' if (p or 0) < 0 else '■'
        return f"{arrow} **{b['symbol']}** {pstr}" + (f" (${spot:.2f})" if spot else "")
    return {
        'title': '📊 Benchmarks (context)',
        'color': 0x6E7781,
        'description': "  ·  ".join(_fmt(b) for b in benchmarks),
        'footer': footer,
    }


def _rs_divergence_embed(divergences: list, bench: str, footer: dict) -> dict | None:
    """Names moving relatively strong/weak INDEPENDENT of the benchmark. None if none."""
    if not divergences:
        return None
    strong = [d for d in divergences if d.get('rs_class') == 'RELATIVELY_STRONG']
    weak   = [d for d in divergences if d.get('rs_class') == 'RELATIVELY_WEAK']
    def _line(d):
        return f"{d['symbol']} {d['rs']:+.2f}%" + (f" (own {d['pct']:+.2f}%)" if d.get('pct') is not None else "")
    parts = []
    if strong:
        parts.append("🟢 **Relatively strong:** " + " · ".join(_line(d) for d in strong))
    if weak:
        parts.append("🔴 **Relatively weak:** " + " · ".join(_line(d) for d in weak))
    return {
        'title': f'⚖️ Relative Strength vs {bench} (divergences)',
        'color': 0x0969DA,
        'description': "\n".join(parts) + f"\n_Raw relative return = stock %chg − {bench} %chg._",
        'footer': footer,
    }


def send_morning_briefing(
    results: list,
    now: datetime,
    oi_buildup: list | None = None,
    weekend_gaps: list | None = None,
    open_positions: dict | None = None,
    benchmarks: list | None = None,
    rs_divergences: list | None = None,
) -> None:
    """
    Send the 8:20 AM morning briefing to Discord as one mobile-first embed per
    symbol — bias-colored border, stacked Support/Resistance fields with rank
    labels beside each value — instead of a wide monospaced table that wraps and
    detaches labels on mobile.

    `results` is the list built in run_morning_snapshot.py / morning_snapshot():
    each item has keys: symbol, prev_close, pm_price, expiry, supports,
    resistances, sentiment (which carries bias + pc_ratio).

    `weekend_gaps` (optional, first session after a weekend/holiday) is a list of
    per-symbol dicts {symbol, prior_session, gap_days, gaps:[...]} — only symbols
    with at least one qualifying gap. Rendered as one trailing 'Weekend OI Gaps'
    embed, one field per symbol.
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

    # Benchmark context (SPY/QQQ) + relative-strength divergences ride up top with
    # the header, before the per-symbol embeds (which each carry their own vs-QQQ tag).
    be = _benchmark_embed(benchmarks or [], footer)
    if be:
        embeds.append(be)
    de = _rs_divergence_embed(rs_divergences or [], config.RS_BENCHMARK, footer)
    if de:
        embeds.append(de)

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

    # Weekend / post-holiday OI gaps: biggest OI changes since the prior session,
    # across this-week + next-week expiries, one field per symbol.
    wg = [w for w in (weekend_gaps or []) if w.get('gaps')]
    if wg:
        spans = {w['prior_session'] for w in wg if w.get('prior_session')}
        since = (f"since {min(spans).strftime('%b %d')}" if len(spans) == 1
                 else "since the prior session")
        embeds.append({
            'title': f'🟦 Weekend OI Gaps ({since})',
            'color': 0x0969DA,
            'description': (f"Largest near-dated OI changes over the {wg[0].get('gap_days', '?')}-day "
                            "market closure (≥ thresholds, ranked)."),
            'fields': [
                {
                    'name': (f"{w['symbol']}"
                             + (f" — {w['gap_days']}d gap" if w.get('gap_days') else "")),
                    'value': "\n".join(_weekend_gap_line(g) for g in w['gaps']) or '—',
                    'inline': False,
                }
                for w in wg
            ],
            'footer': footer,
        })

    # Open-position check: surface anything Alpaca still holds into the session.
    op_embed = _open_positions_embed(open_positions, footer)
    if op_embed:
        embeds.append(op_embed)

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


def send_rs_divergence_alert(symbol: str, rs_val: float, own_pct: float,
                             bench_pct: float, rs_class: str, bench: str) -> None:
    """Intraday note: a name has diverged hard from the benchmark (gated, default off)."""
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return
    strong = rs_class == 'RELATIVELY_STRONG'
    icon   = '🟢' if strong else '🔴'
    color  = 0x2DA44E if strong else 0xCF222E
    word   = 'relatively STRONG' if strong else 'relatively WEAK'
    prefix = "[SAMPLE] " if config.SAMPLE_MODE else ""
    _post(url, {"embeds": [{
        "description": (f"{prefix}{icon} **{symbol}** {word} vs {bench}\n"
                        f"RS **{rs_val:+.2f}%**  ({symbol} {own_pct:+.2f}% · {bench} {bench_pct:+.2f}%)"),
        "color": color,
        "footer": {"text": "Jakevolume · Relative Strength"},
    }]})
    logger.info("Discord: RS divergence %s %s vs %s", symbol, rs_class, bench)


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


def send_research_finding(finding: dict, session_date, finding_id=None) -> None:
    """
    Post a Claude-generated research finding to Discord (§83).
    `finding` is the parsed JSON dict from the nightly pipeline.
    """
    url = (config.DISCORD_RESEARCH_WEBHOOK_URL
           or config.DISCORD_REVIEW_WEBHOOK_URL
           or config.DISCORD_MORNING_WEBHOOK_URL
           or config.DISCORD_WEBHOOK_URL)
    if not url:
        return

    category   = finding.get('category', 'UNKNOWN')
    obs        = finding.get('observation', '')
    expected   = finding.get('expected_benefit', '')
    cost       = finding.get('possible_cost', '')
    conf       = finding.get('confidence')
    conf_str   = f"{conf:.0%}" if conf is not None else "?"
    fid_str    = f" #{finding_id}" if finding_id else ""
    prefix     = "**[SAMPLE]** " if config.SAMPLE_MODE else ""

    proposed   = finding.get('proposed_change_json') or {}
    param      = proposed.get('parameter', '')
    cur_val    = proposed.get('current_value', '')
    new_val    = proposed.get('proposed_value', '')
    change_str = f"`{param}`: {cur_val} → {new_val}" if param else "(see details)"

    lines = [
        f"{prefix}**JAKEVOLUME NIGHTLY RESEARCH — {session_date}{fid_str}**",
        f"**Category:** {category}  |  **Confidence:** {conf_str}",
        f"**Observation:** {obs}",
        f"**Proposed change:** {change_str}",
        f"**Expected benefit:** {expected}",
        f"**Possible cost:** {cost}",
    ]
    ev_ids = finding.get('evidence_ids', '')
    if ev_ids:
        lines.append(f"**Evidence signal IDs:** {ev_ids}")

    _post(url, {"content": "\n".join(lines)})
    logger.info("Discord: research finding sent (category=%s conf=%s)", category, conf_str)


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

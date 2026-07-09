"""
Nightly Claude research pipeline (§81-§83).

Runs post-close, after analyze_daily_signals() has populated signal_analysis,
signal_outcomes, counterfactual_exits, and missed_opportunities for the day.

  §81 — _gather_session_data: aggregate today's performance from all tables
  §82 — _call_claude: build a structured prompt and call the Anthropic API
  §83 — run_nightly_pipeline: persist to research_findings + post to Discord

Disabled gracefully when ANTHROPIC_API_KEY is not set or the `anthropic`
package is not installed.
"""
import json
import logging
from datetime import date as _date
from typing import Optional

import psycopg2

import config
import db.ops as db
from output.discord_notifier import send_research_finding

logger = logging.getLogger(__name__)


def _conn():
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASSWORD,
    )


# ── §81: Data aggregation ─────────────────────────────────────────────────────

def _gather_session_data(session_date: _date) -> dict:
    """Pull today's full performance summary from Phase 0/1 tables."""
    conn = _conn()
    try:
        cur = conn.cursor()

        # Signal analysis joined with outcomes
        cur.execute("""
            SELECT sa.signal_id, sa.symbol, sa.signal_type, sa.entry_price,
                   sa.mfe_pct, sa.mae_pct, sa.rule_pnl_pct, sa.suggested_action,
                   sa.draw_down_magnitude_pct, sa.time_to_mfe_min,
                   sa.profit_capture_efficiency, sa.entry_timing_label,
                   sa.target1_reached, sa.target2_reached,
                   sa.blended_return_pct, sa.ex_post_rr_ratio, sa.capture_label,
                   so.entry_success, so.false_positive,
                   so.reached_100pct, so.reached_200pct
            FROM signal_analysis sa
            LEFT JOIN signal_outcomes so ON sa.signal_id = so.signal_id
            WHERE sa.analysis_date = %s
        """, (session_date,))
        cols = ['signal_id', 'symbol', 'signal_type', 'entry_price',
                'mfe_pct', 'mae_pct', 'rule_pnl_pct', 'suggested_action',
                'draw_down_pct', 'time_to_mfe_min', 'capture_efficiency',
                'entry_timing_label', 'target1_reached', 'target2_reached',
                'blended_return_pct', 'ex_post_rr', 'capture_label',
                'entry_success', 'false_positive', 'reached_100pct', 'reached_200pct']
        signals = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Counterfactual strategy comparison, aggregated across all signals today
        cur.execute("""
            SELECT strategy,
                   ROUND(AVG(return_pct)::numeric, 2)         AS avg_return,
                   ROUND(AVG(capture_efficiency)::numeric, 4) AS avg_eff,
                   COUNT(*)                                    AS n
            FROM counterfactual_exits
            WHERE session_date = %s
            GROUP BY strategy
            ORDER BY avg_return DESC
        """, (session_date,))
        counterfactuals = [
            {'strategy': r[0],
             'avg_return_pct': float(r[1]) if r[1] is not None else None,
             'avg_efficiency': float(r[2]) if r[2] is not None else None,
             'n': r[3]}
            for r in cur.fetchall()
        ]

        # Missed opportunities (top 5 by magnitude)
        cur.execute("""
            SELECT symbol, option_type, level_type, level_rank,
                   maximum_return_pct, time_to_max_min, blocking_reason
            FROM missed_opportunities
            WHERE session_date = %s
            ORDER BY maximum_return_pct DESC NULLS LAST
            LIMIT 5
        """, (session_date,))
        missed = [
            {'symbol': r[0], 'option_type': r[1], 'level_type': r[2],
             'rank': r[3],
             'max_return_pct': float(r[4]) if r[4] is not None else None,
             'time_to_max_min': r[5], 'blocking_reason': r[6]}
            for r in cur.fetchall()
        ]

        # Morning P/C sentiment
        cur.execute("""
            SELECT symbol, pc_ratio, bias FROM morning_sentiment WHERE snap_date = %s
        """, (session_date,))
        sentiment = {
            r[0]: {'pc_ratio': float(r[1]) if r[1] is not None else None, 'bias': r[2]}
            for r in cur.fetchall()
        }

        # Last 5 research findings for context (avoid repeating the same observation)
        cur.execute("""
            SELECT category, observation, confidence, created_at
            FROM research_findings
            ORDER BY created_at DESC LIMIT 5
        """)
        recent = [
            {'category': r[0], 'observation': (r[1] or '')[:120],
             'confidence': float(r[2]) if r[2] is not None else None,
             'date': str(r[3].date())}
            for r in cur.fetchall()
        ]

        return {
            'session_date':       str(session_date),
            'signals':            signals,
            'counterfactuals':    counterfactuals,
            'missed_opportunities': missed,
            'sentiment':          sentiment,
            'recent_findings':    recent,
        }
    finally:
        conn.close()


# ── §82: Prompt construction ──────────────────────────────────────────────────

def _fmt_signals(signals: list) -> str:
    if not signals:
        return "  (none)"
    lines = []
    for s in signals:
        mfe  = f"{s['mfe_pct']:+.0f}%"  if s['mfe_pct']        is not None else "n/a"
        rule = f"{s['rule_pnl_pct']:+.0f}%" if s['rule_pnl_pct'] is not None else "n/a"
        dd   = f"{s['draw_down_pct']:.0f}%" if s['draw_down_pct'] is not None else "n/a"
        t1   = "Y" if s.get('target1_reached') else "N"
        t2   = "Y" if s.get('target2_reached') else "N"
        succ = "Y" if s.get('entry_success')   else "N"
        fp   = "Y" if s.get('false_positive')  else "N"
        tm   = s.get('entry_timing_label') or "?"
        lines.append(
            f"  [{s['signal_id']}] {s['symbol']} {s['signal_type']}: "
            f"MFE={mfe} Rule={rule} DD={dd} "
            f"T1={t1} T2={t2} Succ={succ} FP={fp} Timing={tm}"
        )
    return "\n".join(lines)


def _fmt_cf(cfs: list) -> str:
    if not cfs:
        return "  (no data)"
    lines = []
    for c in cfs:
        avg = f"{c['avg_return_pct']:+.1f}%" if c['avg_return_pct'] is not None else "n/a"
        eff = f"{c['avg_efficiency']:.2f}"   if c['avg_efficiency'] is not None else "n/a"
        lines.append(f"  {c['strategy']:<22} avg={avg:>8}  eff={eff}")
    return "\n".join(lines)


def _fmt_missed(missed: list) -> str:
    if not missed:
        return "  (none)"
    return "\n".join(
        f"  {m['symbol']} {m['option_type']} {m['level_type']}R{m['rank']}: "
        f"+{m['max_return_pct']:.0f}% in {m['time_to_max_min']}min | "
        f"blocked: {m['blocking_reason']}"
        for m in missed if m['max_return_pct'] is not None
    )


def _fmt_sentiment(sent: dict) -> str:
    if not sent:
        return "  (none)"
    return "\n".join(
        f"  {sym}: P/C={v['pc_ratio']:.3f} {v['bias']}"
        for sym, v in sorted(sent.items())
        if v['pc_ratio'] is not None
    )


def _build_prompt(data: dict) -> str:
    n_sig  = len(data['signals'])
    valid  = [s for s in data['signals'] if s['mfe_pct'] is not None]
    n_succ = sum(1 for s in data['signals'] if s.get('entry_success'))
    n_fp   = sum(1 for s in data['signals'] if s.get('false_positive'))
    avg_mfe = (sum(s['mfe_pct'] for s in valid) / len(valid)) if valid else 0.0

    recent_txt = (
        "\n".join(f"  [{r['date']}] {r['category']}: {r['observation']}"
                  for r in data['recent_findings'])
        or "  (none yet)"
    )

    return f"""You are the quantitative research analyst for Jakevolume, a 0DTE options trading system for MAG-7 stocks (AAPL MSFT AMZN GOOGL META NVDA TSLA).

System overview:
- Pre-market: compute S1/S2/S3 (support) and R1/R2/R3 (resistance) levels from open interest
- Intraday: detect volume bursts at those levels; enter CALL at support, PUT at resistance
- Exit rule: sell 1/2 at R2/S2, rest at R3/S3; 50% hard stop; move stop to breakeven after exit 1

DATE: {data['session_date']}
SIGNALS: {n_sig} fired | Entry-success: {n_succ}/{n_sig} | False-positive: {n_fp}/{n_sig} | Avg MFE: {avg_mfe:.1f}%

== MORNING SENTIMENT ==
{_fmt_sentiment(data['sentiment'])}

== SIGNAL PERFORMANCE ==
Format: [id] SYMBOL DIR: MFE RuleP&L DrawDown T1hit T2hit Success FalsePos TimingLabel
{_fmt_signals(data['signals'])}

== EXIT STRATEGY COMPARISON (averaged across today's signals) ==
{_fmt_cf(data['counterfactuals'])}

== MISSED OPPORTUNITIES (>=100% moves that didn't fire a signal) ==
{_fmt_missed(data['missed_opportunities'])}

== RECENT RESEARCH FINDINGS (do NOT duplicate these) ==
{recent_txt}

TASK: Generate exactly ONE research finding or actionable hypothesis from today's data.
Focus on: entry timing patterns, exit strategy improvements, volume threshold calibration,
OI level behavior, P/C sentiment correlation, or risk management.

Return ONLY a valid JSON object — no markdown, no preamble:

{{
  "category": "ENTRY_TIMING | EXIT_STRATEGY | VOLUME_PATTERN | OI_PATTERN | SENTIMENT_CORRELATION | RISK_MANAGEMENT",
  "observation": "Specific observation from today's data (2-3 sentences)",
  "evidence_ids": "Comma-separated signal IDs, e.g. '12,15' or empty string",
  "supporting_metrics_json": {{"metric_name": value}},
  "proposed_change_json": {{"parameter": "name", "current_value": x, "proposed_value": y, "rationale": "..."}},
  "expected_benefit": "Specific measurable improvement (e.g. 'reduce false positives by ~20%')",
  "possible_cost": "Risk or downside of this change",
  "backtest_request_json": {{"test_description": "...", "metric_to_measure": "...", "min_sample_size": N}},
  "confidence": 0.0
}}"""


# ── §82: Claude API call ──────────────────────────────────────────────────────

def _call_claude(prompt: str) -> Optional[dict]:
    """Call the Anthropic API and parse the JSON finding from the response."""
    try:
        import anthropic
    except ImportError:
        logger.error("Nightly pipeline: 'anthropic' package not installed — run: pip install anthropic")
        return None

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model=config.NIGHTLY_PIPELINE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences when present
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Nightly pipeline: Claude response was not valid JSON", exc_info=True)
        return None
    except Exception:
        logger.warning("Nightly pipeline: Anthropic API call failed", exc_info=True)
        return None


# ── §83: Orchestrator ─────────────────────────────────────────────────────────

def run_nightly_pipeline(session_date: _date) -> bool:
    """
    §81-§83 entry point. Returns True when a finding was successfully saved.

    Called from main.py after analyze_daily_signals() completes. Silently
    no-ops when ANTHROPIC_API_KEY is absent, the 'anthropic' package is missing,
    or there are no signals to analyze.
    """
    if not config.ANTHROPIC_API_KEY:
        logger.info("Nightly pipeline: skipped (ANTHROPIC_API_KEY not set)")
        return False

    logger.info("Nightly pipeline: gathering session data for %s", session_date)
    try:
        data = _gather_session_data(session_date)
    except Exception:
        logger.warning("Nightly pipeline: data aggregation failed", exc_info=True)
        return False

    if not data['signals']:
        logger.info("Nightly pipeline: no signals on %s — skipping", session_date)
        return False

    prompt = _build_prompt(data)
    logger.info("Nightly pipeline: calling %s", config.NIGHTLY_PIPELINE_MODEL)

    finding = _call_claude(prompt)
    if not finding:
        return False

    # §83: Persist
    finding_row = {
        'session_date':            session_date,
        'category':                finding.get('category'),
        'observation':             finding.get('observation'),
        'evidence_ids':            finding.get('evidence_ids'),
        'supporting_metrics_json': finding.get('supporting_metrics_json'),
        'proposed_change_json':    finding.get('proposed_change_json'),
        'expected_benefit':        finding.get('expected_benefit'),
        'possible_cost':           finding.get('possible_cost'),
        'backtest_request_json':   finding.get('backtest_request_json'),
        'confidence':              finding.get('confidence'),
        'status':                  'PROPOSED',
    }
    fid = None
    try:
        fid = db.save_research_finding(finding_row)
        logger.info("Nightly pipeline: finding #%d saved — %s (conf=%.2f)",
                    fid, finding.get('category', '?'), float(finding.get('confidence') or 0))
    except Exception:
        logger.warning("Nightly pipeline: save_research_finding failed", exc_info=True)

    # §83: Deliver to Discord
    try:
        send_research_finding(finding, session_date, fid)
    except Exception:
        logger.warning("Nightly pipeline: Discord delivery failed", exc_info=True)

    return fid is not None

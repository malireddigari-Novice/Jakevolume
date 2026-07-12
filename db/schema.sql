-- jakevolume — PostgreSQL schema
-- Run once; idempotent (IF NOT EXISTS / ON CONFLICT).

-- ── Full option chain snapshots (one row per contract per day) ─────────────
CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    id               BIGSERIAL    PRIMARY KEY,
    symbol           VARCHAR(10)  NOT NULL,
    snap_date        DATE         NOT NULL,
    snap_time        TIMESTAMPTZ  NOT NULL,
    expiry_date      DATE         NOT NULL,
    strike           NUMERIC(12,4) NOT NULL,
    option_type      VARCHAR(4)   NOT NULL CHECK (option_type IN ('CALL','PUT')),
    open_interest    BIGINT       NOT NULL DEFAULT 0,
    volume           BIGINT       NOT NULL DEFAULT 0,
    bid              NUMERIC(12,4),
    ask              NUMERIC(12,4),
    mark             NUMERIC(12,4),
    underlying_price NUMERIC(12,4),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ocs_symbol_date
    ON option_chain_snapshots (symbol, snap_date);

-- ── Near-dated multi-expiry OI snapshots (weekend-gap detection) ───────────
-- Deliberately separate from option_chain_snapshots: that table is normalized to
-- the single nearest expiry and several queries (e.g. get_oi_changes_today) collapse
-- by (strike, option_type) ignoring expiry. Storing multiple expiries here keeps the
-- weekend-gap feature from corrupting the nearest-expiry buildup/watchlist logic.
-- One row per (symbol, snap_date, expiry, strike, option_type); weekend gaps are
-- computed at query time by comparing two snap_dates.
CREATE TABLE IF NOT EXISTS near_oi_snapshots (
    id               BIGSERIAL    PRIMARY KEY,
    symbol           VARCHAR(10)  NOT NULL,
    snap_date        DATE         NOT NULL,
    snap_time        TIMESTAMPTZ  NOT NULL,
    expiry_date      DATE         NOT NULL,
    strike           NUMERIC(12,4) NOT NULL,
    option_type      VARCHAR(4)   NOT NULL CHECK (option_type IN ('CALL','PUT')),
    open_interest    BIGINT       NOT NULL DEFAULT 0,
    underlying_price NUMERIC(12,4),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, snap_date, expiry_date, strike, option_type)
);

CREATE INDEX IF NOT EXISTS idx_nos_symbol_date
    ON near_oi_snapshots (symbol, snap_date);

-- ── Daily OI levels (3 S + 3 R per symbol, computed at 08:00 CST) ──────────
CREATE TABLE IF NOT EXISTS oi_levels (
    id            BIGSERIAL    PRIMARY KEY,
    symbol        VARCHAR(10)  NOT NULL,
    level_date    DATE         NOT NULL,
    level_type    VARCHAR(10)  NOT NULL CHECK (level_type IN ('SUPPORT','RESISTANCE')),
    rank          SMALLINT     NOT NULL CHECK (rank BETWEEN 1 AND 10),
    strike        NUMERIC(12,4) NOT NULL,
    open_interest BIGINT       NOT NULL,
    option_type   VARCHAR(4)   NOT NULL CHECK (option_type IN ('CALL','PUT')),
    computed_at   TIMESTAMPTZ  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, level_date, level_type, rank)
);

CREATE INDEX IF NOT EXISTS idx_oil_symbol_date
    ON oi_levels (symbol, level_date);

-- ── Intraday 1-minute price bars ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_bars (
    id         BIGSERIAL    PRIMARY KEY,
    symbol     VARCHAR(10)  NOT NULL,
    bar_time   TIMESTAMPTZ  NOT NULL,
    open       NUMERIC(12,4) NOT NULL,
    high       NUMERIC(12,4) NOT NULL,
    low        NUMERIC(12,4) NOT NULL,
    close      NUMERIC(12,4) NOT NULL,
    volume     BIGINT       NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_pb_symbol_time
    ON price_bars (symbol, bar_time DESC);

-- Per-stock extras (idempotent): underlying spot at bar close + cumulative
-- session volume. price_bars.volume is the per-minute candle volume.
-- 7 stored fields per bar: open, high (max), low (min), close, volume (per-min),
-- spot_price, cum_volume (running session total).
ALTER TABLE price_bars ADD COLUMN IF NOT EXISTS spot_price NUMERIC(12,4);
ALTER TABLE price_bars ADD COLUMN IF NOT EXISTS cum_volume BIGINT;

-- ── 1-minute OHLCV for each S/R level's option contract ────────────────────
-- One row per minute per level (6 levels/symbol: S1-S3, R1-R3). Sourced from
-- Schwab price-history on the OCC option symbol; ON CONFLICT keeps the latest
-- candle values so the forming minute settles to its final volume.
CREATE TABLE IF NOT EXISTS option_level_bars (
    id          BIGSERIAL     PRIMARY KEY,
    symbol      VARCHAR(10)   NOT NULL,
    level_date  DATE          NOT NULL,
    level_type  VARCHAR(10)   NOT NULL CHECK (level_type IN ('SUPPORT','RESISTANCE')),
    rank        SMALLINT      NOT NULL CHECK (rank BETWEEN 1 AND 10),
    strike      NUMERIC(12,4) NOT NULL,
    option_type VARCHAR(4)    NOT NULL CHECK (option_type IN ('CALL','PUT')),
    expiry      DATE          NOT NULL,
    occ_symbol  VARCHAR(30)   NOT NULL,
    bar_time    TIMESTAMPTZ   NOT NULL,
    open        NUMERIC(12,4) NOT NULL,
    high        NUMERIC(12,4) NOT NULL,
    low         NUMERIC(12,4) NOT NULL,
    close       NUMERIC(12,4) NOT NULL,
    volume      BIGINT        NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (occ_symbol, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_olb_symbol_date_time
    ON option_level_bars (symbol, level_date, bar_time DESC);

-- ── 1-hour OHLCV history for each S/R level's option contract ───────────────
-- Pulled once per trading day in morning_snapshot from Alpaca (1Hour bars over
-- the trailing OPT_HOURLY_LOOKBACK_DAYS). One row per hourly candle per contract;
-- snap_date is the morning the pull ran. ON CONFLICT (occ_symbol, bar_time) keeps
-- the latest candle values so an overlapping next-day pull self-corrects. Pruned
-- with the other bar data (by bar_time) so storage stays bounded.
CREATE TABLE IF NOT EXISTS option_hourly_bars (
    id          BIGSERIAL     PRIMARY KEY,
    symbol      VARCHAR(10)   NOT NULL,
    snap_date   DATE          NOT NULL,
    level_type  VARCHAR(10)   NOT NULL CHECK (level_type IN ('SUPPORT','RESISTANCE')),
    rank        SMALLINT      NOT NULL CHECK (rank BETWEEN 1 AND 10),
    strike      NUMERIC(12,4) NOT NULL,
    option_type VARCHAR(4)    NOT NULL CHECK (option_type IN ('CALL','PUT')),
    expiry      DATE          NOT NULL,
    occ_symbol  VARCHAR(30)   NOT NULL,
    bar_time    TIMESTAMPTZ   NOT NULL,
    open        NUMERIC(12,4) NOT NULL,
    high        NUMERIC(12,4) NOT NULL,
    low         NUMERIC(12,4) NOT NULL,
    close       NUMERIC(12,4) NOT NULL,
    volume      BIGINT        NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (occ_symbol, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_ohb_symbol_snap
    ON option_hourly_bars (symbol, snap_date);
CREATE INDEX IF NOT EXISTS idx_ohb_occ_time
    ON option_hourly_bars (occ_symbol, bar_time DESC);

-- ── Fired signals ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                 BIGSERIAL    PRIMARY KEY,
    symbol             VARCHAR(10)  NOT NULL,
    signal_time        TIMESTAMPTZ  NOT NULL,
    signal_type        VARCHAR(10)  NOT NULL CHECK (signal_type IN ('BULLISH','BEARISH')),
    bias               VARCHAR(30)  NOT NULL,
    level_type         VARCHAR(10)  NOT NULL,
    level_price        NUMERIC(12,4) NOT NULL,
    trigger_price      NUMERIC(12,4) NOT NULL,
    avg_volume_20      NUMERIC(18,2),
    spike_volume       BIGINT,
    consecutive_spikes SMALLINT,
    option_type        VARCHAR(4),
    opt_mark           NUMERIC(12,4),
    opt_bid            NUMERIC(12,4),
    opt_ask            NUMERIC(12,4),
    opt_vol_delta      BIGINT,
    sheets_logged      BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Idempotent column additions (safe to re-run on every startup)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS option_type    VARCHAR(4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS opt_mark       NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS opt_bid        NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS opt_ask        NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS opt_vol_delta  BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS price_to_enter NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS price_to_exit  NUMERIC(12,4);

-- Steps 2-9 cluster detection fields
ALTER TABLE signals ADD COLUMN IF NOT EXISTS prox_score      NUMERIC(6,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cluster_strength NUMERIC(6,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS strong_cluster  BOOLEAN;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS flow_shape      VARCHAR(15);
-- Spec single-print/cluster classification + confidence tier
ALTER TABLE signals ALTER COLUMN flow_shape TYPE VARCHAR(25);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_shape    VARCHAR(25);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS confidence      VARCHAR(15);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cluster_active_bars SMALLINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cluster_burst_bars  SMALLINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS upgrade             BOOLEAN NOT NULL DEFAULT FALSE;
-- §1 production signal context: PRIMARY_LEVEL_CONTINUATION /
-- PRIMARY_LEVEL_COUNTERTREND_REVERSAL / CHAIN_LED_EMERGENT_ENTRY
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_context        VARCHAR(36);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS emergent_location_id  BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS target1_oi_name       VARCHAR(4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS target2_oi_name       VARCHAR(4);
-- Next-day-expiry (Tue/Thu) mode: interchangeable levels + OTM target strike
ALTER TABLE signals ADD COLUMN IF NOT EXISTS day_mode      VARCHAR(10);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS traded_strike NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS target_level  NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS atm_vol_1m      BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS atm_spike_ratio NUMERIC(8,2);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS atm_vol_3m      BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS itm_vol_1m      BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS itm_spike_ratio NUMERIC(8,2);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS itm_vol_3m      BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS spread_pct      NUMERIC(8,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS low_dist        NUMERIC(8,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS room_score      NUMERIC(6,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS room_pct        NUMERIC(8,6);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS pc_ratio        NUMERIC(8,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS pc_conviction   VARCHAR(15);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS option_hl_flag  VARCHAR(15);

-- ── Gold-only production mode (P1) ─────────────────────────────────────────────
-- signal_context widened to hold Gold subtypes; gold_grade routes production vs
-- research-only; value/clow regions (§12/§13); intent/veto reserved for P2.
ALTER TABLE signals ALTER COLUMN signal_context TYPE VARCHAR(48);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS gold_grade      VARCHAR(12);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS gold_subtype    VARCHAR(48);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS value_region    VARCHAR(28);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS clow_region     VARCHAR(48);
-- Fix P1 undersize: 'ACCEPTABLE_ONLY_WITH_EXCEPTIONAL_EVIDENCE' is 41 chars and
-- overflowed the original VARCHAR(40), crashing save_signal (and the whole
-- intraday_check for that symbol) for any signal with low_dist in (1.50, 1.75].
ALTER TABLE signals ALTER COLUMN clow_region TYPE VARCHAR(48);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS intent_class    VARCHAR(40);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS opp_veto        VARCHAR(48);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS call_leadership NUMERIC(6,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS put_leadership  NUMERIC(6,4);

CREATE INDEX IF NOT EXISTS idx_sig_symbol_time
    ON signals (symbol, signal_time DESC);

-- ── Event-time state per signal (P-ET) — market state frozen at threshold-cross ──
CREATE TABLE IF NOT EXISTS signal_event_state (
    signal_id                     BIGINT PRIMARY KEY REFERENCES signals(id),
    symbol                        VARCHAR(10),
    contract_strike               NUMERIC(12,4),
    option_type                   VARCHAR(4),
    event_start_time              TIMESTAMPTZ,
    spot_at_event_start           NUMERIC(12,4),
    atm_strike_at_event_start     NUMERIC(12,4),
    strike_distance_at_event      NUMERIC(12,4),
    threshold_cross_time          TIMESTAMPTZ,
    spot_at_threshold_cross       NUMERIC(12,4),
    atm_strike_at_threshold_cross NUMERIC(12,4),
    bid_at_threshold              NUMERIC(12,4),
    ask_at_threshold              NUMERIC(12,4),
    last_at_threshold             NUMERIC(12,4),
    r60_at_threshold              BIGINT,
    r180_at_threshold             BIGINT,
    observed_volume_at_decision   BIGINT,
    final_revised_volume          BIGINT,
    decision_timestamp            TIMESTAMPTZ,
    no_retro_label                VARCHAR(32),
    bid_at_commit                 NUMERIC(12,4),
    ask_at_commit                 NUMERIC(12,4),
    mid_at_commit                 NUMERIC(12,4),
    paper_fill_price              NUMERIC(12,4),
    paper_fill_method             VARCHAR(20),
    price_moved_from_event        BOOLEAN,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS final_revised_volume BIGINT;
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS no_retro_label       VARCHAR(32);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS bid_at_commit          NUMERIC(12,4);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS ask_at_commit          NUMERIC(12,4);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS mid_at_commit          NUMERIC(12,4);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS paper_fill_price       NUMERIC(12,4);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS paper_fill_method      VARCHAR(20);
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS price_moved_from_event  BOOLEAN;
ALTER TABLE signal_event_state ADD COLUMN IF NOT EXISTS commit_time             TIMESTAMPTZ;

-- ── §17 Signal latency (flow-event → alert timing per signal) ────────────────
CREATE TABLE IF NOT EXISTS signal_latency (
    signal_id             BIGINT PRIMARY KEY REFERENCES signals(id),
    symbol                VARCHAR(10),
    event_start_time      TIMESTAMPTZ,
    threshold_cross_time  TIMESTAMPTZ,
    commit_time           TIMESTAMPTZ,
    bar_wait_secs         NUMERIC(10,1),   -- WATCH → THRESHOLD cross
    commit_lag_secs       NUMERIC(10,1),   -- THRESHOLD cross → commit
    total_latency_secs    NUMERIC(10,1),   -- WATCH cross → commit (end-to-end)
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── §13 Gate-by-gate audit (which production gate admitted/blocked a signal) ──
CREATE TABLE IF NOT EXISTS signal_gate_audit (
    signal_id      BIGINT PRIMARY KEY REFERENCES signals(id),
    symbol         VARCHAR(10),
    decision       VARCHAR(12),    -- PRODUCTION | RESEARCH
    blocking_gate  VARCHAR(20),    -- first FAIL gate, NULL when admitted
    summary        TEXT,           -- one-line 'GATE:VERDICT ... → DECISION' render
    gates          JSONB,          -- ordered [{gate, verdict, detail}]
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Morning sentiment (daily P/C ratio + bias per symbol) ────────────────────
CREATE TABLE IF NOT EXISTS morning_sentiment (
    id          BIGSERIAL    PRIMARY KEY,
    symbol      VARCHAR(10)  NOT NULL,
    snap_date   DATE         NOT NULL,
    pc_ratio    NUMERIC(8,4) NOT NULL,
    bias        VARCHAR(20)  NOT NULL,
    computed_at TIMESTAMPTZ  NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, snap_date)
);

-- ── Volume cluster positioning monitor ───────────────────────────────────────
-- Tracks unusual option volume accumulation patterns without firing signals.
-- pattern_type SAME_DAY_MOVER    : single 1-min bar ≥ CLUSTER_VOL_0DTE on 0DTE
-- pattern_type NEXT_EXPIRY_POSITIONING : rolling N-bar total ≥ CLUSTER_VOL_NEXT on next expiry
CREATE TABLE IF NOT EXISTS volume_clusters (
    id                      BIGSERIAL     PRIMARY KEY,
    symbol                  VARCHAR(10)   NOT NULL,
    detected_at             TIMESTAMPTZ   NOT NULL,
    updated_at              TIMESTAMPTZ   NOT NULL,
    pattern_type            VARCHAR(30)   NOT NULL
        CHECK (pattern_type IN ('SAME_DAY_MOVER','NEXT_EXPIRY_POSITIONING')),
    option_type             VARCHAR(4)    NOT NULL CHECK (option_type IN ('CALL','PUT')),
    strike                  NUMERIC(12,4) NOT NULL,
    expiry                  DATE          NOT NULL,
    underlying_price        NUMERIC(12,4) NOT NULL,
    cluster_volume          BIGINT        NOT NULL,
    bar_count               SMALLINT      NOT NULL DEFAULT 1,
    avg_vol_per_bar         NUMERIC(18,2),
    status                  VARCHAR(10)   NOT NULL
        CHECK (status IN ('FORMING','CONFIRMED','FADED')),
    nearest_sr_level        VARCHAR(10),
    nearest_sr_strike       NUMERIC(12,4),
    distance_from_price_pct NUMERIC(8,4),
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vc_symbol_status
    ON volume_clusters (symbol, status, updated_at DESC);

-- ── Alpaca trade executions ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                 BIGSERIAL     PRIMARY KEY,
    signal_id          BIGINT        REFERENCES signals(id),
    symbol             VARCHAR(10)   NOT NULL,
    occ_symbol         VARCHAR(30)   NOT NULL,
    alpaca_order_id    VARCHAR(50),
    side               VARCHAR(10)   NOT NULL DEFAULT 'buy',
    qty                INT           NOT NULL,
    limit_price        NUMERIC(12,4) NOT NULL,
    buying_power_used  NUMERIC(14,2),
    paper              BOOLEAN       NOT NULL DEFAULT TRUE,
    status             VARCHAR(20)   NOT NULL DEFAULT 'placed',
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_signal
    ON trades (signal_id);

-- Exit tracking columns (idempotent)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS signal_type       VARCHAR(10);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit1_underlying  NUMERIC(12,4);  -- R1 (BULLISH) or S1 (BEARISH)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit2_underlying  NUMERIC(12,4);  -- R2 (BULLISH) or S2 (BEARISH)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit1_qty         INT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit2_qty         INT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit1_filled      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit2_filled      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit1_filled_at   TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit2_filled_at   TIMESTAMPTZ;
-- Stoploss tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS stoploss_price    NUMERIC(12,4);  -- option mark level; moves to entry after exit1
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strike            NUMERIC(12,4);  -- option strike (for mark lookup)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS option_type       VARCHAR(4);     -- CALL or PUT
ALTER TABLE trades ADD COLUMN IF NOT EXISTS expiry            DATE;           -- option expiry; NULL = 0DTE (close at EOD)

-- ── Daily post-close signal review (15:00 CST) ─────────────────────────────────
-- One row per signal per day: realized intraday excursions of the traded contract
-- plus a SUGGESTED management action (e.g. take 30% + move stop to breakeven) and
-- the % that suggestion would have captured. Written by analysis/daily_review.py.
CREATE TABLE IF NOT EXISTS signal_analysis (
    id                BIGSERIAL     PRIMARY KEY,
    signal_id         BIGINT        REFERENCES signals(id),
    analysis_date     DATE          NOT NULL,
    symbol            VARCHAR(10)   NOT NULL,
    signal_time       TIMESTAMPTZ   NOT NULL,
    signal_type       VARCHAR(10)   NOT NULL,
    traded_strike     NUMERIC(12,4),
    option_type       VARCHAR(4),
    entry_price       NUMERIC(12,4),
    mfe_pct           NUMERIC(8,2),   -- max favorable excursion (peak gain %)
    mae_pct           NUMERIC(8,2),   -- max adverse excursion (worst drawdown %)
    peak_price        NUMERIC(12,4),
    peak_time         TIMESTAMPTZ,
    trough_price      NUMERIC(12,4),
    rule_pnl_pct      NUMERIC(8,2),   -- outcome of the CURRENT live exit rule
    suggested_action  VARCHAR(40),    -- e.g. TAKE_50@30_BE_TRAIL
    suggested_pnl_pct NUMERIC(8,2),   -- % the suggested management would have captured
    suggestion        TEXT,           -- human-readable management note
    data_source       VARCHAR(12),    -- where the price path came from
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (signal_id)
);
CREATE INDEX IF NOT EXISTS idx_sig_analysis_date ON signal_analysis (analysis_date);

-- ── Flow Leadership Reversals ──────────────────────────────────────────────────
-- One row each time the reversal engine flips an open position's flow story: the
-- position exited, plus the HYPOTHETICAL opposite entry (contract + mark) so the
-- reversal's value can be measured later (paper-track; auto-flip off in V1).
CREATE TABLE IF NOT EXISTS flow_reversals (
    id                BIGSERIAL     PRIMARY KEY,
    symbol            VARCHAR(10)   NOT NULL,
    detected_at       TIMESTAMPTZ   NOT NULL,
    trade_id          BIGINT        REFERENCES trades(id),
    from_side         VARCHAR(4)    NOT NULL,   -- the side being exited (CALL/PUT)
    to_side           VARCHAR(4)    NOT NULL,   -- the opposite side now leading
    spot              NUMERIC(12,4),
    exit_occ          VARCHAR(30),
    exit_price        NUMERIC(12,4),            -- mark of the exited position contract
    same_leadership   NUMERIC(6,3),
    opp_leadership    NUMERIC(6,3),
    leadership_diff   NUMERIC(6,3),
    opp_burst         NUMERIC(8,2),
    opp_share         NUMERIC(8,3),
    hypo_occ          VARCHAR(30),              -- hypothetical opposite entry contract
    hypo_strike       NUMERIC(12,4),
    hypo_entry_price  NUMERIC(12,4),            -- opposite contract mark at reversal
    flipped           BOOLEAN       NOT NULL DEFAULT FALSE,  -- auto-flip opened the opp trade
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_flow_reversals_sym_date
    ON flow_reversals (symbol, detected_at);

-- ── Objective outcome labels per signal (§20-§24) ──────────────────────────────
-- Python-computed, label-free outcomes for every fired signal: forward returns,
-- excursions, level-reach flags, and the EntrySuccess / FalsePositive labels. The
-- raw returns are stored regardless of the label so labels can be redefined later.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id            BIGINT       PRIMARY KEY REFERENCES signals(id),
    session_date         DATE         NOT NULL,
    symbol               VARCHAR(10),
    entry_price          NUMERIC(12,4),
    return_5m            NUMERIC(8,2),
    return_15m           NUMERIC(8,2),
    return_30m           NUMERIC(8,2),
    return_60m           NUMERIC(8,2),
    return_eod           NUMERIC(8,2),
    mfe_pct              NUMERIC(8,2),
    mae_pct              NUMERIC(8,2),
    reached_50pct        BOOLEAN,
    reached_100pct       BOOLEAN,
    reached_200pct       BOOLEAN,
    entry_success        BOOLEAN,     -- +50% before -35% within 30m
    strong_entry_success BOOLEAN,     -- +100% before -35% within 60m
    false_positive       BOOLEAN,     -- fails +25% AND hits -35% within 30m
    contract_lod         NUMERIC(12,4),  -- traded contract's intraday low-of-day
    entry_vs_lod         NUMERIC(8,3),   -- entry / LOD (1.0 = bought the exact low; >1 = chased)
    pct_peak_captured    NUMERIC(8,1),   -- current-rule P&L as a % of the peak move (MFE)
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_date ON signal_outcomes (session_date);
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS contract_lod      NUMERIC(12,4);
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS entry_vs_lod      NUMERIC(8,3);
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS pct_peak_captured NUMERIC(8,1);

-- ── Phase 1: extended signal_analysis columns (§57-§66) ────────────────────────
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS absolute_day_low          NUMERIC(12,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS absolute_day_low_time     TIMESTAMPTZ;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS absolute_day_high         NUMERIC(12,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS absolute_day_high_time    TIMESTAMPTZ;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS pre_alert_low             NUMERIC(12,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS post_alert_low            NUMERIC(12,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS post_alert_low_time       TIMESTAMPTZ;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS draw_down_magnitude_pct   NUMERIC(8,2);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS time_to_mfe_min           SMALLINT;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS time_to_mae_min           SMALLINT;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS time_underwater_min       SMALLINT;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS blended_return_pct        NUMERIC(8,2);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS profit_capture_efficiency NUMERIC(8,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS profit_left_on_table_pct  NUMERIC(8,2);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS capture_label             VARCHAR(20);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS entry_above_lod_pct      NUMERIC(8,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS entry_timing_score        NUMERIC(8,4);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS entry_timing_label        VARCHAR(30);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS possible_early_entry      BOOLEAN;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS strong_early_entry_warning BOOLEAN;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS possible_bad_entry        BOOLEAN;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS ex_post_rr_ratio          NUMERIC(8,2);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS realized_rr_ratio         NUMERIC(8,2);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS target1_reached           BOOLEAN;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS target1_reached_time      TIMESTAMPTZ;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS target2_reached           BOOLEAN;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS target2_reached_time      TIMESTAMPTZ;
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS target1_capture_label     VARCHAR(20);
ALTER TABLE signal_analysis ADD COLUMN IF NOT EXISTS target2_capture_label     VARCHAR(20);

-- ── Phase 1: extended signal_outcomes columns (§71) ─────────────────────────────
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS return_1m       NUMERIC(8,2);
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS return_3m       NUMERIC(8,2);
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS reached_25pct   BOOLEAN;
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS reached_500pct  BOOLEAN;
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS time_to_mfe_min SMALLINT;
ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS time_to_mae_min SMALLINT;

-- ── Phase 1: counterfactual exit comparison (§68) ───────────────────────────────
-- One row per signal per strategy: compare 8 alternative exit rules on the
-- realized option price path. 'CURRENT_RULE' is the production baseline.
CREATE TABLE IF NOT EXISTS counterfactual_exits (
    id                 BIGSERIAL    PRIMARY KEY,
    signal_id          BIGINT       REFERENCES signals(id),
    session_date       DATE         NOT NULL,
    strategy           VARCHAR(20)  NOT NULL,    -- SELL_ALL_T1 / HALF_T1_HALF_T2 / etc.
    return_pct         NUMERIC(8,2),
    capture_efficiency NUMERIC(8,4),
    diff_from_actual   NUMERIC(8,2),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (signal_id, strategy)
);
CREATE INDEX IF NOT EXISTS idx_cf_exits_date ON counterfactual_exits (session_date);

-- ── Phase 1: entry delay simulation (§69) ───────────────────────────────────────
-- Simulate entering 1/2/3/5 minutes later than the actual alert time.
CREATE TABLE IF NOT EXISTS entry_delay_study (
    id                          BIGSERIAL    PRIMARY KEY,
    signal_id                   BIGINT       REFERENCES signals(id),
    session_date                DATE         NOT NULL,
    delay_min                   SMALLINT     NOT NULL,   -- 1 / 2 / 3 / 5
    delayed_entry_price         NUMERIC(12,4),
    mfe_pct                     NUMERIC(8,2),
    mae_pct                     NUMERIC(8,2),
    rule_pnl_pct                NUMERIC(8,2),
    capture_efficiency          NUMERIC(8,4),
    move_missed_before_entry_pct NUMERIC(8,2), -- (delayed_price/orig_price - 1)*100
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (signal_id, delay_min)
);
CREATE INDEX IF NOT EXISTS idx_entry_delay_date ON entry_delay_study (session_date);

-- ── Phase 1: missed opportunity scanner (§72) ───────────────────────────────────
-- Contracts in option_level_bars that rose >= 100% from a local low within 30 min
-- without triggering a signal. Populated post-close by analyze_daily_signals.
CREATE TABLE IF NOT EXISTS missed_opportunities (
    id                  BIGSERIAL    PRIMARY KEY,
    session_date        DATE         NOT NULL,
    symbol              VARCHAR(10)  NOT NULL,
    occ_symbol          VARCHAR(30),
    strike              NUMERIC(12,4),
    option_type         VARCHAR(4),
    level_type          VARCHAR(10),
    level_rank          SMALLINT,
    event_start_time    TIMESTAMPTZ,
    local_low_price     NUMERIC(12,4),
    maximum_price       NUMERIC(12,4),
    maximum_return_pct  NUMERIC(8,1),
    time_to_max_min     SMALLINT,
    blocking_reason     VARCHAR(48),   -- from signal_candidates, or NOT_EVALUATED
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_missed_opps_date ON missed_opportunities (session_date, symbol);

-- ── Phase 3: Secondary OI Watchlist (§11-§16) ─────────────────────────────────
-- Extended monitoring beyond the primary 6 S/R levels.  Three tiers:
--   EXTENDED_RANK : additional ranks (4+) within the primary ±OI_LEVEL_BAND_PCT
--   OUTER_WALL    : top-N by OI in the outer band (primary→SECONDARY_OUTER_BAND_PCT)
--   OI_BUILDUP    : top-N by overnight oi_change across ALL strikes (new positioning)
-- Populated at 8:20 AM after reconcile_oi_changes runs for the day.
CREATE TABLE IF NOT EXISTS secondary_oi_levels (
    id             BIGSERIAL    PRIMARY KEY,
    symbol         VARCHAR(10)  NOT NULL,
    level_date     DATE         NOT NULL,
    watchlist_tier VARCHAR(20)  NOT NULL
        CHECK (watchlist_tier IN ('EXTENDED_RANK','OUTER_WALL','OI_BUILDUP')),
    strike         NUMERIC(12,4) NOT NULL,
    option_type    VARCHAR(4)   NOT NULL CHECK (option_type IN ('CALL','PUT')),
    open_interest  INT,
    oi_change      INT,
    oi_change_pct  NUMERIC(8,4),
    distance_pct   NUMERIC(8,4),            -- (strike-spot)/spot; negative = put below spot
    band_rank      SMALLINT,                -- 1 = highest OI or biggest oi_change in tier
    expiry         DATE,
    computed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, level_date, watchlist_tier, strike, option_type)
);
CREATE INDEX IF NOT EXISTS idx_secondary_oi_date ON secondary_oi_levels (symbol, level_date);

-- ── Phase 4: Per-minute call/put leadership scores (§41) ──────────────────────
-- Computed every minute for every symbol from the watched option contracts.
-- Same formula as the flow-reversal engine's _leadership() but recorded for ALL
-- symbols continuously, not only those with open positions.
CREATE TABLE IF NOT EXISTS volume_leadership (
    id               BIGSERIAL    PRIMARY KEY,
    symbol           VARCHAR(10)  NOT NULL,
    bar_time         TIMESTAMPTZ  NOT NULL,
    session_date     DATE         NOT NULL,
    call_leadership  NUMERIC(8,4),
    put_leadership   NUMERIC(8,4),
    leadership_diff  NUMERIC(8,4),   -- call - put; positive = calls dominating
    dominant_side    VARCHAR(7),     -- CALL | PUT | NEUTRAL
    call_vol_5m      BIGINT,
    put_vol_5m       BIGINT,
    spot             NUMERIC(12,4),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_vol_lead_date ON volume_leadership (symbol, session_date);

-- ── Relative strength vs benchmark (QQQ) — MAG7 independence from the index ────
-- Raw relative return RS = stock %change - benchmark %change. One MORNING row per
-- symbol at the snapshot, plus INTRADAY rows tracked alongside MAG7 during the day.
CREATE TABLE IF NOT EXISTS relative_strength (
    id            BIGSERIAL   PRIMARY KEY,
    symbol        VARCHAR(10) NOT NULL,
    session_date  DATE        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    scope         VARCHAR(10) NOT NULL,     -- MORNING | INTRADAY
    bench_symbol  VARCHAR(10) NOT NULL,
    stock_pct     NUMERIC(8,3),             -- % change vs its reference (prev_close / open)
    bench_pct     NUMERIC(8,3),
    rs            NUMERIC(8,3),             -- stock_pct - bench_pct (pct points)
    rs_class      VARCHAR(20),              -- RELATIVELY_STRONG | RELATIVELY_WEAK | IN_LINE
    spot          NUMERIC(12,4),
    bench_spot    NUMERIC(12,4),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rs_symbol_date ON relative_strength (symbol, session_date);
CREATE INDEX IF NOT EXISTS idx_rs_scope_date  ON relative_strength (scope, session_date);

-- ── ATM 0DTE capture (both sides, premium only — NOT OI) ──────────────────────
-- The at-the-money call + put for the front (0DTE) expiry at the morning snapshot,
-- chosen by proximity to spot (0DTE OI is near-zero and useless). Strike + bid/ask/mark.
CREATE TABLE IF NOT EXISTS atm_0dte_snapshots (
    id           BIGSERIAL   PRIMARY KEY,
    symbol       VARCHAR(10) NOT NULL,
    snap_date    DATE        NOT NULL,
    snap_time    TIMESTAMPTZ NOT NULL,
    expiry       DATE,
    spot         NUMERIC(12,4),
    call_strike  NUMERIC(12,4),
    call_bid     NUMERIC(12,4),
    call_ask     NUMERIC(12,4),
    call_mark    NUMERIC(12,4),
    put_strike   NUMERIC(12,4),
    put_bid      NUMERIC(12,4),
    put_ask      NUMERIC(12,4),
    put_mark     NUMERIC(12,4),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, snap_date)
);
CREATE INDEX IF NOT EXISTS idx_atm0dte_date ON atm_0dte_snapshots (symbol, snap_date);

-- ── Phase 4: Signal volume analytics (§26-§29, §31 — post-session per signal) ─
-- Multi-timeframe aggregation, shape classification, entropy, chain breakdown,
-- and migration direction for the option-volume window at each alert.
CREATE TABLE IF NOT EXISTS signal_volume_analytics (
    id                     BIGSERIAL    PRIMARY KEY,
    signal_id              BIGINT       REFERENCES signals(id),
    session_date           DATE         NOT NULL,
    symbol                 VARCHAR(10)  NOT NULL,
    -- §26 Multi-timeframe volumes
    vol_2m                 BIGINT,
    vol_3m                 BIGINT,
    vol_5m                 BIGINT,
    vol_10m                BIGINT,
    vol_15m                BIGINT,
    vol_30m                BIGINT,
    ratio_2m               NUMERIC(8,2),
    ratio_5m               NUMERIC(8,2),
    ratio_10m              NUMERIC(8,2),
    ratio_15m              NUMERIC(8,2),
    ratio_30m              NUMERIC(8,2),
    -- §27 Volume shape features
    volume_shape           VARCHAR(20),
    shape_hhi              NUMERIC(8,4),
    burst_ratio            NUMERIC(8,4),
    staircase_score        NUMERIC(8,4),
    -- §28 Volume entropy
    normalized_entropy     NUMERIC(8,4),
    -- §29 Chain-relative volume
    atm_vol_share          NUMERIC(8,4),
    itm_vol_share          NUMERIC(8,4),
    otm_vol_share          NUMERIC(8,4),
    strike_volume_center   NUMERIC(12,4),
    center_vs_spot         NUMERIC(8,4),
    -- §31 Volume migration
    vol_center_change      NUMERIC(8,4),
    vol_migration_direction VARCHAR(20),
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (signal_id)
);
CREATE INDEX IF NOT EXISTS idx_sva_date ON signal_volume_analytics (session_date);

-- ── Phase 4: Historical strike-volume event memory (§32) ──────────────────────
-- One row per detected volume event (single-print / cluster / staircase) whether
-- or not it led to a signal.  Forward returns are backfilled post-session from
-- option_level_bars so each event has an outcome for ML training.
CREATE TABLE IF NOT EXISTS volume_events (
    id              BIGSERIAL    PRIMARY KEY,
    symbol          VARCHAR(10)  NOT NULL,
    session_date    DATE         NOT NULL,
    event_time      TIMESTAMPTZ  NOT NULL,
    occ_symbol      VARCHAR(30),
    strike          NUMERIC(12,4),
    option_type     VARCHAR(4),
    expiry          DATE,
    event_type      VARCHAR(20),      -- SINGLE_PRINT | CLUSTER | STAIRCASE
    trigger_volume  BIGINT,
    trigger_ratio   NUMERIC(8,2),
    mark_at_event   NUMERIC(12,4),
    low_dist        NUMERIC(8,4),
    volume_shape    VARCHAR(20),
    normalized_entropy NUMERIC(8,4),
    led_to_signal   BOOLEAN      NOT NULL DEFAULT FALSE,
    signal_id       BIGINT       REFERENCES signals(id),
    return_5m       NUMERIC(8,2),
    return_15m      NUMERIC(8,2),
    return_30m      NUMERIC(8,2),
    mfe_pct         NUMERIC(8,2),
    mae_pct         NUMERIC(8,2),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_vol_events_sym_date ON volume_events (symbol, session_date);
CREATE INDEX IF NOT EXISTS idx_vol_events_occ      ON volume_events (occ_symbol, session_date);

-- ── Phase 5: OI event reconciliation + position intent (§18, §34-§37) ──────────
-- oi_events: one row per major volume event detected intraday.  Populated post-session
-- by daily_review.py with an initial Bayesian-rule intent estimate; reconciled the
-- next morning (morning_snapshot) once the overnight OI change is confirmed.
CREATE TABLE IF NOT EXISTS oi_events (
    id                      BIGSERIAL    PRIMARY KEY,
    symbol                  VARCHAR(10)  NOT NULL,
    session_date            DATE         NOT NULL,
    event_time              TIMESTAMPTZ  NOT NULL,
    occ_symbol              VARCHAR(30),
    strike                  NUMERIC(12,4),
    option_type             VARCHAR(4),
    expiry                  DATE,
    -- Raw event context (§34 evidence inputs)
    trigger_volume          BIGINT,
    mark_at_event           NUMERIC(12,4),
    low_dist                NUMERIC(8,4),   -- mark/session_low ratio
    high_ratio              NUMERIC(8,4),   -- mark/session_high ratio
    volume_shape            VARCHAR(20),
    event_type              VARCHAR(20),    -- SINGLE_PRINT | CLUSTER | STAIRCASE
    time_of_day_frac        NUMERIC(6,4),   -- 0=open, 1=close
    -- Live intent prediction (§34-§35, set post-session)
    live_intent             VARCHAR(30),    -- OPENING_CALL_BUYING etc.
    intent_probability      NUMERIC(8,4),
    intent_confidence       VARCHAR(10),    -- HIGH | MEDIUM | LOW
    supporting_evidence     TEXT,
    contradicting_evidence  TEXT,
    -- Next-day reconciliation (§37, updated in morning_snapshot)
    confirmed_oi_change     INT,
    confirmed_oi_change_pct NUMERIC(8,4),
    reconciled_intent       VARCHAR(25),    -- CONFIRMED_OPENING | CONFIRMED_CLOSING | NO_CHANGE
    prediction_correct      BOOLEAN,
    reconciled_at           TIMESTAMPTZ,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, session_date, event_time, strike, option_type)
);
CREATE INDEX IF NOT EXISTS idx_oi_events_date ON oi_events (symbol, session_date);
CREATE INDEX IF NOT EXISTS idx_oi_events_unreconciled
    ON oi_events (symbol, session_date) WHERE reconciled_at IS NULL;

-- position_lifecycle: probable open → close pairs for the same contract within a session.
-- Built post-session when 2+ major volume events occur on the same contract.
-- Buyer lifecycle:  large vol near low → OI↑ → premium expands → large vol near high → OI↓
-- Seller lifecycle: large vol near high → OI↑ → premium collapses → vol near low → OI↓
CREATE TABLE IF NOT EXISTS position_lifecycle (
    id                       BIGSERIAL    PRIMARY KEY,
    symbol                   VARCHAR(10)  NOT NULL,
    session_date             DATE         NOT NULL,
    occ_symbol               VARCHAR(30),
    strike                   NUMERIC(12,4),
    option_type              VARCHAR(4),
    expiry                   DATE,
    -- Opening event
    open_event_id            BIGINT       REFERENCES oi_events(id),
    probable_open_time       TIMESTAMPTZ,
    probable_open_price      NUMERIC(12,4),
    probable_open_volume     BIGINT,
    probable_position_type   VARCHAR(30),
    opening_probability      NUMERIC(8,4),
    -- Intraday price excursion between events
    maximum_contract_price   NUMERIC(12,4),
    minimum_contract_price   NUMERIC(12,4),
    -- Closing event (NULL if only an opening was detected)
    close_event_id           BIGINT       REFERENCES oi_events(id),
    probable_close_time      TIMESTAMPTZ,
    probable_close_price     NUMERIC(12,4),
    probable_close_volume    BIGINT,
    closing_probability      NUMERIC(8,4),
    -- Outcome (backfilled from next-morning oi_events reconciliation)
    confirmed_oi_change      INT,
    lifecycle_return_pct     NUMERIC(8,2),
    confidence               VARCHAR(10),
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, session_date, strike, option_type)
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_date ON position_lifecycle (symbol, session_date);

-- ── Claude research journal (§30/§49) ──────────────────────────────────────────
-- Home for Claude's structured findings/hypotheses. Claude proposes; humans approve;
-- backtests validate. No write access to production from here — this is the journal.
CREATE TABLE IF NOT EXISTS research_findings (
    finding_id              BIGSERIAL    PRIMARY KEY,
    session_date            DATE,
    category                VARCHAR(40),
    observation             TEXT,
    evidence_ids            TEXT,
    supporting_metrics_json JSONB,
    proposed_change_json    JSONB,
    expected_benefit        TEXT,
    possible_cost           TEXT,
    backtest_request_json   JSONB,
    confidence              NUMERIC(4,2),
    status                  VARCHAR(16)  NOT NULL DEFAULT 'PROPOSED',
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    reviewed_at             TIMESTAMPTZ,
    reviewed_by             VARCHAR(40)
);
CREATE INDEX IF NOT EXISTS idx_research_findings_status ON research_findings (status);

-- ── Phase 0: Greeks + cumulative volume on option_level_bars (§8, §38, §39) ────
-- Captured once per minute from Alpaca OPRA snapshots; NULL for historical bars
-- where the level was not in the nearest-n watch set at that minute.
-- COALESCE in the upsert preserves the first non-NULL value per bar as time passes.
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS implied_vol       NUMERIC(10,6);  -- decimal (0.324 = 32.4% IV)
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS delta             NUMERIC(8,5);
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS gamma             NUMERIC(10,7);
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS vega              NUMERIC(8,5);
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS theta             NUMERIC(8,5);
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS rho               NUMERIC(8,5);
ALTER TABLE option_level_bars ADD COLUMN IF NOT EXISTS cum_option_volume BIGINT;         -- running session option volume

-- ── Phase 0: Greeks + OI change on option_chain_snapshots (§17) ─────────────────
-- Greeks from Schwab morning chain (IV stored as decimal, e.g. 0.324 = 32.4%).
-- OI change columns populated by db.reconcile_oi_changes() after morning save.
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS delta              NUMERIC(8,5);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS gamma              NUMERIC(10,7);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS vega               NUMERIC(8,5);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS theta              NUMERIC(8,5);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS rho                NUMERIC(8,5);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS implied_vol        NUMERIC(10,6);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS prev_open_interest BIGINT;       -- yesterday's OI for same contract
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS oi_change          BIGINT;       -- today_oi - yesterday_oi
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS oi_change_pct      NUMERIC(10,4);-- oi_change / max(yesterday_oi, 1)
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS volume_to_oi       NUMERIC(10,4);-- today_volume / max(yesterday_oi, 1)

-- ── Per-candidate evaluation log (§73) ─────────────────────────────────────────
-- One row per S/R level evaluated per poll — every blocked candidate AND every passed
-- one — so precision/recall, block-reason analysis, and missed-opportunity studies have
-- the full denominator (not only fired alerts). Pruned with the other 1-min data.
CREATE TABLE IF NOT EXISTS signal_candidates (
    id                    BIGSERIAL    PRIMARY KEY,
    ts                    TIMESTAMPTZ  NOT NULL,
    session_date          DATE         NOT NULL,
    symbol                VARCHAR(10)  NOT NULL,
    candidate_side        VARCHAR(4),               -- CALL / PUT (confirm side)
    level_label           VARCHAR(4),               -- S1..R3
    strike                NUMERIC(12,4),
    spot                  NUMERIC(12,4),
    dist_pct              NUMERIC(8,4),
    near_level            BOOLEAN,
    contract_low_distance NUMERIC(8,4),
    contract_near_low     BOOLEAN,
    valid_volume_event    BOOLEAN,
    already_alerted       BOOLEAN,
    alert_fired           BOOLEAN      NOT NULL DEFAULT FALSE,
    signal_type           VARCHAR(10),
    blocked_reason        VARCHAR(48),              -- PASSED / NOT_NEAR_LEVEL / NO_VALID_VOLUME_SIGNAL:* / ...
    hv_pctile             NUMERIC(8,4),
    atm_vol_1m            BIGINT,
    win_vol               BIGINT,
    active_bars           SMALLINT,
    -- ── Production two-path volume gate (§12/§15) ──
    gate_path             VARCHAR(2),               -- A (dominant) / B (contextual) / NULL
    gold_standard         BOOLEAN,
    pending               BOOLEAN,                  -- PENDING_VOLUME_CONFIRMATION
    trigger_volume        BIGINT,                   -- volume of the qualifying shape (used)
    trigger_ratio         NUMERIC(10,2),
    premium_notional      NUMERIC(16,2),            -- trigger_vol × mark × 100
    peak_1m               BIGINT,
    vol_3m                BIGINT,
    vol_5m                BIGINT,
    event_share           NUMERIC(6,3),
    persistent_bg         BOOLEAN,
    bar_status            VARCHAR(12),              -- PARTIAL / COMPLETED / REVISED
    observed_vol          BIGINT,                   -- live poll-delta at evaluation
    completed_vol         BIGINT,                   -- closed 1-min OPRA bar volume
    classification        TEXT,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- Additive columns for already-existing signal_candidates tables.
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS gate_path        VARCHAR(2);
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS gold_standard    BOOLEAN;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS pending          BOOLEAN;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS trigger_volume   BIGINT;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS trigger_ratio    NUMERIC(10,2);
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS premium_notional NUMERIC(16,2);
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS peak_1m          BIGINT;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS vol_3m           BIGINT;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS vol_5m           BIGINT;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS event_share      NUMERIC(6,3);
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS persistent_bg    BOOLEAN;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS bar_status       VARCHAR(12);
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS observed_vol     BIGINT;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS completed_vol    BIGINT;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS classification   TEXT;
CREATE INDEX IF NOT EXISTS idx_sig_candidates_date  ON signal_candidates (session_date, symbol);
CREATE INDEX IF NOT EXISTS idx_sig_candidates_fired ON signal_candidates (session_date, alert_fired);
CREATE INDEX IF NOT EXISTS idx_sig_candidates_gold  ON signal_candidates (session_date, gold_standard);

-- ── Phase 6: Statistical Research Toolkit (§74-§77) ─────────────────────────

-- §74 Permutation test results — one row per (test_name, session_date, symbol)
CREATE TABLE IF NOT EXISTS research_permutation_tests (
    id               BIGSERIAL    PRIMARY KEY,
    session_date     DATE,
    symbol           VARCHAR(10),
    test_name        VARCHAR(60)  NOT NULL,   -- e.g. 'signal_5m_return_vs_random'
    metric           VARCHAR(20)  NOT NULL,   -- 'mean' | 'median' | 'sharpe'
    n_observed       INT,
    n_control        INT,
    n_permutations   INT,
    observed_metric  NUMERIC(14,6),
    null_mean        NUMERIC(14,6),
    null_std         NUMERIC(14,6),
    p_value          NUMERIC(10,6),
    effect_size      NUMERIC(10,4),           -- Cohen's d
    percentile_rank  NUMERIC(8,4),
    ci_lower         NUMERIC(14,6),
    ci_upper         NUMERIC(14,6),
    significant      BOOLEAN,                 -- p_value < 0.05
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_perm_tests_date ON research_permutation_tests (session_date, test_name);

-- §75 Monte Carlo results — one row per simulation run
CREATE TABLE IF NOT EXISTS research_monte_carlo (
    id                       BIGSERIAL    PRIMARY KEY,
    session_date             DATE         NOT NULL,
    symbol                   VARCHAR(10),
    n_trades                 INT,
    n_simulations            INT,
    starting_capital         NUMERIC(12,2),
    expected_return          NUMERIC(12,4),
    median_return            NUMERIC(12,4),
    probability_of_loss      NUMERIC(8,4),
    probability_of_ruin      NUMERIC(8,4),
    target_hit_probability   NUMERIC(8,4),
    max_drawdown_p5          NUMERIC(10,4),
    max_drawdown_p50         NUMERIC(10,4),
    max_drawdown_p95         NUMERIC(10,4),
    ci_lower_95              NUMERIC(12,4),
    ci_upper_95              NUMERIC(12,4),
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mc_date ON research_monte_carlo (session_date);

-- §77 Change-point detections — one row per (symbol, session_date, analysis window)
CREATE TABLE IF NOT EXISTS research_change_points (
    id                          BIGSERIAL    PRIMARY KEY,
    session_date                DATE         NOT NULL,
    symbol                      VARCHAR(10)  NOT NULL,
    option_side                 VARCHAR(4),            -- CALL | PUT | COMBINED
    n_bars                      INT,
    n_breakpoints               INT,
    breakpoint_indices          JSONB,                 -- [bar_index, ...]
    pre_regime_mean             NUMERIC(14,4),
    post_regime_mean            NUMERIC(14,4),
    regime_change_ratio         NUMERIC(10,4),
    concentrated_event_detected BOOLEAN,
    model_used                  VARCHAR(20),
    pen                         NUMERIC(8,2),
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cp_date ON research_change_points (symbol, session_date);

-- ── §16-17 Gate control library — regression safety net ───────────────────────
-- Known-good (POSITIVE) and spam-like (NEGATIVE) volume events. Every gate change
-- is re-run against these: positive controls MUST still pass, negative MUST block.
CREATE TABLE IF NOT EXISTS gate_controls (
    id               BIGSERIAL PRIMARY KEY,
    control_type     VARCHAR(8)  NOT NULL,         -- POSITIVE / NEGATIVE
    control_label    VARCHAR(48) NOT NULL,         -- GOLD_STANDARD_LEVEL_REJECTION / SPAM_* / ...
    symbol           VARCHAR(10) NOT NULL,
    strike           NUMERIC(12,4),
    option_type      VARCHAR(4),
    alert_time       TIMESTAMPTZ,
    spot             NUMERIC(12,4),
    level_label      VARCHAR(4),
    level_price      NUMERIC(12,4),
    entry_price      NUMERIC(12,4),                -- option mark
    vols             JSONB NOT NULL,               -- per-minute volume deltas (oldest→newest)
    observed_vol     BIGINT,
    completed_vol    BIGINT,
    ratio            NUMERIC(10,2),
    event_share      NUMERIC(6,3),
    premium_notional NUMERIC(16,2),
    low_dist         NUMERIC(8,4),
    is_atm           BOOLEAN DEFAULT TRUE,
    next_day_mode    BOOLEAN DEFAULT FALSE,
    expected_pass    BOOLEAN NOT NULL,             -- POSITIVE→true / NEGATIVE→false
    expected_path    VARCHAR(2),
    expected_gold    BOOLEAN,
    target1          NUMERIC(12,4),
    target2          NUMERIC(12,4),
    target1_reached  BOOLEAN,
    target2_reached  BOOLEAN,
    bid_mfe_pct      NUMERIC(10,2),
    bid_mae_pct      NUMERIC(10,2),
    time_to_mfe_min  INTEGER,
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (control_label, symbol, strike, alert_time)
);
CREATE INDEX IF NOT EXISTS idx_gate_controls_type ON gate_controls (control_type);

-- ── §6 Emergent intraday locations (chain-led entry reference) ─────────────────
CREATE TABLE IF NOT EXISTS emergent_locations (
    id                BIGSERIAL PRIMARY KEY,
    session_date      DATE        NOT NULL,
    symbol            VARCHAR(10) NOT NULL,
    location_type     VARCHAR(12),                 -- SUPPORT (calls) / RESISTANCE (puts)
    location_spot     NUMERIC(12,4),               -- spot at the start of the qualifying chain event
    direction         VARCHAR(10),                 -- BULLISH / BEARISH
    event_start       TIMESTAMPTZ,
    event_end         TIMESTAMPTZ,
    atm_strike        NUMERIC(12,4),
    itm_strike        NUMERIC(12,4),
    otm_strike        NUMERIC(12,4),
    atm_vol_3m        BIGINT,
    itm_vol_3m        BIGINT,
    otm_vol_3m        BIGINT,
    combined_vol_3m   BIGINT,
    atm_notional      NUMERIC(16,2),
    combined_notional NUMERIC(16,2),
    atm_low_dist      NUMERIC(8,4),
    itm_low_dist      NUMERIC(8,4),
    otm_low_dist      NUMERIC(8,4),
    call_leadership   NUMERIC(6,3),
    put_leadership    NUMERIC(6,3),
    selected_strike   NUMERIC(12,4),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_emergent_date ON emergent_locations (session_date, symbol);

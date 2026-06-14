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

CREATE INDEX IF NOT EXISTS idx_sig_symbol_time
    ON signals (symbol, signal_time DESC);

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

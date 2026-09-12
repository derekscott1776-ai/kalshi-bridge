BEGIN;

-- ============================================================
-- KALSHI AI - INITIAL POSTGRESQL SCHEMA
-- Migration: 001_initial_schema.sql
-- Purpose:
--   Persist scanner runs, recommendations, actual positions,
--   market snapshots, risk settings, and future CLV/P&L data.
-- ============================================================


-- ============================================================
-- 1. SETTINGS
-- ============================================================

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- 2. SCANS
-- One record for each automatic scanner run.
-- ============================================================

CREATE TABLE IF NOT EXISTS scans (
    id BIGSERIAL PRIMARY KEY,

    scan_uuid UUID NOT NULL UNIQUE,

    sport TEXT NOT NULL,
    market_type TEXT NOT NULL,

    scan_date DATE NOT NULL,

    model_version TEXT NOT NULL,

    home_field_elo NUMERIC(10,4),
    elo_scale NUMERIC(10,4),

    bankroll_dollars NUMERIC(12,2),
    max_position_dollars NUMERIC(12,2),
    max_open_risk_dollars NUMERIC(12,2),

    markets_discovered INTEGER NOT NULL DEFAULT 0,
    markets_analyzed INTEGER NOT NULL DEFAULT 0,
    buy_recommendations INTEGER NOT NULL DEFAULT 0,
    pass_recommendations INTEGER NOT NULL DEFAULT 0,

    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),

    error_message TEXT,

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- 3. RECOMMENDATIONS
-- Immutable snapshot of what the model recommended at scan time.
-- ============================================================

CREATE TABLE IF NOT EXISTS recommendations (
    id BIGSERIAL PRIMARY KEY,

    scan_id BIGINT NOT NULL
        REFERENCES scans(id)
        ON DELETE CASCADE,

    sport TEXT NOT NULL,
    market_type TEXT NOT NULL,

    game_date DATE,

    team TEXT,
    opponent TEXT,

    event_ticker TEXT NOT NULL,
    ticker TEXT NOT NULL,

    side TEXT
        CHECK (side IN ('YES', 'NO')),

    economic_exposure_key TEXT NOT NULL,

    model_version TEXT NOT NULL,

    fair_probability NUMERIC(12,8),

    bid_price NUMERIC(12,8),
    ask_price NUMERIC(12,8),
    last_price NUMERIC(12,8),

    recommended_position_dollars NUMERIC(12,2),
    recommended_contracts NUMERIC(18,6),

    weighted_fill_price NUMERIC(12,8),

    gross_contract_cost NUMERIC(14,6),
    estimated_fee NUMERIC(14,6),
    all_in_cost NUMERIC(14,6),
    all_in_cost_per_contract NUMERIC(14,8),

    gross_edge NUMERIC(12,8),
    net_edge NUMERIC(12,8),
    net_roi NUMERIC(12,8),
    expected_profit NUMERIC(14,6),

    spread_dollars NUMERIC(12,8),
    slippage_dollars NUMERIC(12,8),
    liquidity_coverage NUMERIC(18,6),

    full_fill BOOLEAN,

    decision TEXT NOT NULL
        CHECK (decision IN ('BUY YES', 'BUY NO', 'PASS')),

    failure_reason TEXT,

    recommendation_rank INTEGER,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Prevent multiple BUY recommendations representing the same
-- economic exposure during the same scan.
--
-- Example:
-- Michigan YES and Oklahoma NO represent the same underlying
-- Michigan-win exposure and should not both become BUY records.

CREATE UNIQUE INDEX IF NOT EXISTS
    uq_recommendations_scan_buy_exposure
ON recommendations (
    scan_id,
    economic_exposure_key
)
WHERE decision IN ('BUY YES', 'BUY NO');


CREATE INDEX IF NOT EXISTS
    idx_recommendations_scan_id
ON recommendations(scan_id);


CREATE INDEX IF NOT EXISTS
    idx_recommendations_ticker
ON recommendations(ticker);


CREATE INDEX IF NOT EXISTS
    idx_recommendations_event_ticker
ON recommendations(event_ticker);


CREATE INDEX IF NOT EXISTS
    idx_recommendations_created_at
ON recommendations(created_at);


CREATE INDEX IF NOT EXISTS
    idx_recommendations_decision
ON recommendations(decision);


-- ============================================================
-- 4. POSITIONS
-- Created only when an actual trade is taken.
-- Recommendations remain historical model records.
-- ============================================================

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,

    recommendation_id BIGINT NOT NULL
        REFERENCES recommendations(id)
        ON DELETE RESTRICT,

    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (
            status IN (
                'OPEN',
                'CLOSED',
                'SETTLED',
                'CANCELLED'
            )
        ),

    actual_contracts NUMERIC(18,6),

    actual_entry_price NUMERIC(12,8),
    actual_entry_fee NUMERIC(14,6),
    actual_entry_cost NUMERIC(14,6),

    actual_exit_price NUMERIC(12,8),
    actual_exit_fee NUMERIC(14,6),
    actual_exit_value NUMERIC(14,6),

    outcome TEXT
        CHECK (
            outcome IS NULL
            OR outcome IN ('WIN', 'LOSS', 'VOID')
        ),

    settlement_value NUMERIC(14,6),

    realized_profit_loss NUMERIC(14,6),
    realized_roi NUMERIC(12,8),

    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX IF NOT EXISTS
    idx_positions_status
ON positions(status);


CREATE INDEX IF NOT EXISTS
    idx_positions_recommendation_id
ON positions(recommendation_id);


CREATE INDEX IF NOT EXISTS
    idx_positions_opened_at
ON positions(opened_at);


-- ============================================================
-- 5. MARKET SNAPSHOTS
-- Stores market state at entry, close, and settlement.
-- This provides the foundation for CLV analysis.
-- ============================================================

CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY,

    recommendation_id BIGINT NOT NULL
        REFERENCES recommendations(id)
        ON DELETE CASCADE,

    snapshot_type TEXT NOT NULL
        CHECK (
            snapshot_type IN (
                'ENTRY',
                'CLOSE',
                'SETTLEMENT'
            )
        ),

    bid_price NUMERIC(12,8),
    ask_price NUMERIC(12,8),
    last_price NUMERIC(12,8),

    volume NUMERIC(20,6),
    volume_24h NUMERIC(20,6),
    open_interest NUMERIC(20,6),

    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX IF NOT EXISTS
    idx_market_snapshots_recommendation_id
ON market_snapshots(recommendation_id);


CREATE INDEX IF NOT EXISTS
    idx_market_snapshots_type
ON market_snapshots(snapshot_type);


CREATE INDEX IF NOT EXISTS
    idx_market_snapshots_captured_at
ON market_snapshots(captured_at);


CREATE UNIQUE INDEX IF NOT EXISTS
    uq_market_snapshots_exact
ON market_snapshots (
    recommendation_id,
    snapshot_type,
    captured_at
);


-- ============================================================
-- UPDATED_AT TRIGGER FUNCTION
-- ============================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- Recreate triggers safely if this migration is rerun.

DROP TRIGGER IF EXISTS
    trg_settings_updated_at
ON settings;

CREATE TRIGGER trg_settings_updated_at
BEFORE UPDATE ON settings
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();


DROP TRIGGER IF EXISTS
    trg_scans_updated_at
ON scans;

CREATE TRIGGER trg_scans_updated_at
BEFORE UPDATE ON scans
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();


DROP TRIGGER IF EXISTS
    trg_positions_updated_at
ON positions;

CREATE TRIGGER trg_positions_updated_at
BEFORE UPDATE ON positions
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- INITIAL KALSHI AI SETTINGS
-- ============================================================

INSERT INTO settings (
    key,
    value,
    description
)
VALUES

(
    'bankroll_dollars',
    '800',
    'Starting bankroll used by the V1 risk engine.'
),

(
    'max_position_dollars',
    '80',
    'Maximum permitted risk on a single position.'
),

(
    'max_open_risk_dollars',
    '240',
    'Maximum total simultaneous open risk.'
),

(
    'minimum_net_roi',
    '0.05',
    'Minimum required net expected ROI after fees and execution costs.'
),

(
    'minimum_edge_20',
    '0.03',
    'Minimum net edge required for the $20 position tier.'
),

(
    'minimum_edge_40',
    '0.04',
    'Minimum net edge required for the $40 position tier.'
),

(
    'minimum_edge_60',
    '0.05',
    'Minimum net edge required for the $60 position tier.'
),

(
    'minimum_edge_80',
    '0.07',
    'Minimum net edge required for the $80 position tier.'
),

(
    'max_spread_dollars',
    '0.05',
    'Maximum permitted bid-ask spread.'
),

(
    'max_slippage_dollars',
    '0.02',
    'Maximum permitted execution slippage.'
),

(
    'minimum_liquidity_coverage',
    '1.50',
    'Minimum executable book depth relative to desired position size.'
),

(
    'live_home_field_elo',
    '80',
    'Live college-football Elo home-field adjustment.'
),

(
    'live_elo_scale',
    '546',
    'Live college-football Elo logistic scale validated by walk-forward testing.'
)

ON CONFLICT (key)
DO UPDATE SET

    value = EXCLUDED.value,
    description = EXCLUDED.description,
    updated_at = NOW();


COMMIT;


-- ============================================================
-- ROLLBACK REFERENCE
-- ============================================================
--
-- Run separately only if this entire initial schema ever needs
-- to be intentionally removed:
--
-- DROP TABLE IF EXISTS market_snapshots CASCADE;
-- DROP TABLE IF EXISTS positions CASCADE;
-- DROP TABLE IF EXISTS recommendations CASCADE;
-- DROP TABLE IF EXISTS scans CASCADE;
-- DROP TABLE IF EXISTS settings CASCADE;
-- DROP FUNCTION IF EXISTS set_updated_at() CASCADE;
--
-- ============================================================

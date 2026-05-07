-- ==========================================================================
-- Stubber Monitor — schema migration (v0.4 fleet shape, Way B register-once)
-- ==========================================================================
-- Run this ONCE in the Supabase SQL editor.
--
-- What it does:
--   1. Drops the old single-agent schema (checks, issues)
--   2. Creates new agents table — registered once per agent
--   3. Creates new checks table — daily check results, references agents
--
-- Old data: discarded (you confirmed only demo data exists).
-- ==========================================================================

-- ── 1. Drop old tables ──────────────────────────────────────────────────
DROP TABLE IF EXISTS issues CASCADE;
DROP TABLE IF EXISTS checks CASCADE;

-- ── 2. agents table ─────────────────────────────────────────────────────
-- Registered ONCE per agent via POST /agents. The dashboard joins to this
-- when rendering. Daily checks reference agents by agent_id (text).
CREATE TABLE agents (
    agent_id      TEXT PRIMARY KEY,                 -- slug: 'rep-stock-manager'
    name          TEXT NOT NULL,                    -- display: 'Rep Stock Manager'
    description   TEXT,                             -- one-line role
    model         TEXT,                             -- 'claude-opus-4-7'
    template_uuid TEXT,                             -- Stubber template UUID
    deployed_at   DATE,                             -- when agent went live
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 3. checks table ─────────────────────────────────────────────────────
-- One row per daily check posted by Stubber. checked_at is when Stubber's
-- scheduler fired; received_at is when this service got the POST.
-- The (agent_id, checked_at::date) unique constraint means re-posting the
-- same day's check upserts rather than duplicates.
CREATE TABLE checks (
    id             BIGSERIAL PRIMARY KEY,
    agent_id       TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    checked_at     TIMESTAMPTZ NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verdict        TEXT NOT NULL CHECK (verdict IN ('healthy','degraded','down')),
    stubs_total    INTEGER,
    stubs_flagged  INTEGER,
    issues         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{severity, message}]
    raw            JSONB                                  -- full POST payload
);

CREATE INDEX idx_checks_agent_checked ON checks(agent_id, checked_at DESC);

-- One check per agent per calendar day (SAST). Re-POST upserts.
CREATE UNIQUE INDEX uniq_checks_agent_day
    ON checks(agent_id, (date_trunc('day', checked_at AT TIME ZONE 'Africa/Johannesburg')));

-- ── done ────────────────────────────────────────────────────────────────
SELECT 'Migration complete. agents and checks tables ready.' AS status;

-- =============================================================================
-- PostgreSQL Audit Trail Schema for mssql-to-pg-parity-kit
-- Target Engine: PostgreSQL 16+
-- Purpose: Persist immutable migration parity runs and atomic check findings
--          for cutover governance, trend analysis, and compliance auditing.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS migration_audit;

-- -----------------------------------------------------------------------------
-- 1. migration_run: One row per offline parity comparison execution
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS migration_audit.migration_run (
    run_id               UUID PRIMARY KEY,
    source_engine        VARCHAR(32)  NOT NULL CHECK (source_engine IN ('mssql', 'postgresql')),
    source_host          VARCHAR(128) NOT NULL,
    source_db            VARCHAR(128) NOT NULL,
    target_engine        VARCHAR(32)  NOT NULL CHECK (target_engine IN ('mssql', 'postgresql')),
    target_host          VARCHAR(128) NOT NULL,
    target_db            VARCHAR(128) NOT NULL,
    snapshot_captured_at TIMESTAMPTZ  NOT NULL,
    started_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    overall_status       VARCHAR(16)  NOT NULL CHECK (overall_status IN ('PASS', 'WARN', 'FAIL')),
    total_checks         INTEGER      NOT NULL CHECK (total_checks >= 0),
    pass_count           INTEGER      NOT NULL CHECK (pass_count >= 0),
    warn_count           INTEGER      NOT NULL CHECK (warn_count >= 0),
    fail_count           INTEGER      NOT NULL CHECK (fail_count >= 0),
    skip_count           INTEGER      NOT NULL DEFAULT 0 CHECK (skip_count >= 0),
    metadata             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_migration_run_counts_sum
        CHECK (pass_count + warn_count + fail_count + skip_count = total_checks)
);

CREATE INDEX IF NOT EXISTS idx_migration_run_started_at
    ON migration_audit.migration_run (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_migration_run_target_db_status
    ON migration_audit.migration_run (target_db, overall_status, started_at DESC);

-- -----------------------------------------------------------------------------
-- 2. check_result: Atomic per-object parity findings across all 5 layers
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS migration_audit.check_result (
    result_id     BIGSERIAL    PRIMARY KEY,
    run_id        UUID         NOT NULL
                               REFERENCES migration_audit.migration_run (run_id)
                               ON DELETE CASCADE,
    layer         VARCHAR(32)  NOT NULL CHECK (layer IN ('naming', 'structure', 'data', 'routines', 'security')),
    check_code    VARCHAR(64)  NOT NULL,
    severity      VARCHAR(16)  NOT NULL CHECK (severity IN ('INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    status        VARCHAR(16)  NOT NULL CHECK (status IN ('PASS', 'WARN', 'FAIL', 'SKIP')),
    object_type   VARCHAR(32)  NOT NULL,
    source_object TEXT,
    target_object TEXT,
    source_value  TEXT,
    target_value  TEXT,
    message       TEXT         NOT NULL,
    remediation   TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_check_result_run_layer
    ON migration_audit.check_result (run_id, layer, status);

-- Partial index optimized for cutover dashboards querying only active drift
CREATE INDEX IF NOT EXISTS idx_check_result_active_drift
    ON migration_audit.check_result (run_id, severity, layer)
    WHERE status IN ('FAIL', 'WARN');

-- -----------------------------------------------------------------------------
-- 3. Summary Views for Cutover Readiness & Drift Triage
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW migration_audit.v_run_layer_summary AS
SELECT
    r.run_id,
    r.source_db,
    r.target_db,
    r.started_at,
    r.overall_status,
    c.layer,
    COUNT(*)                                       AS total_checks,
    COUNT(*) FILTER (WHERE c.status = 'PASS')      AS pass_count,
    COUNT(*) FILTER (WHERE c.status = 'WARN')      AS warn_count,
    COUNT(*) FILTER (WHERE c.status = 'FAIL')      AS fail_count,
    COUNT(*) FILTER (WHERE c.status = 'SKIP')      AS skip_count,
    COUNT(*) FILTER (WHERE c.severity = 'CRITICAL' AND c.status = 'FAIL') AS critical_failures
FROM migration_audit.migration_run AS r
JOIN migration_audit.check_result AS c
  ON c.run_id = r.run_id
GROUP BY
    r.run_id,
    r.source_db,
    r.target_db,
    r.started_at,
    r.overall_status,
    c.layer;

CREATE OR REPLACE VIEW migration_audit.v_latest_drift AS
WITH latest_run AS (
    SELECT run_id
    FROM migration_audit.migration_run
    ORDER BY started_at DESC
    LIMIT 1
)
SELECT
    c.run_id,
    c.layer,
    c.status,
    c.severity,
    c.check_code,
    c.object_type,
    c.source_object,
    c.target_object,
    c.source_value,
    c.target_value,
    c.message,
    c.remediation
FROM migration_audit.check_result AS c
JOIN latest_run AS lr
  ON lr.run_id = c.run_id
WHERE c.status IN ('FAIL', 'WARN')
ORDER BY
    CASE c.status WHEN 'FAIL' THEN 0 ELSE 1 END,
    CASE c.severity
        WHEN 'CRITICAL' THEN 0
        WHEN 'HIGH'     THEN 1
        WHEN 'MEDIUM'   THEN 2
        WHEN 'LOW'      THEN 3
        ELSE 4
    END,
    c.layer,
    c.check_code;

-- -----------------------------------------------------------------------------
-- 4. Least-Privilege Role Template (optional; uncomment for production deployment)
-- -----------------------------------------------------------------------------
-- CREATE ROLE migration_audit_writer NOLOGIN;
-- CREATE ROLE migration_audit_reader NOLOGIN;
-- GRANT USAGE ON SCHEMA migration_audit TO migration_audit_writer, migration_audit_reader;
-- GRANT SELECT, INSERT ON migration_audit.migration_run, migration_audit.check_result TO migration_audit_writer;
-- GRANT USAGE, SELECT ON SEQUENCE migration_audit.check_result_result_id_seq TO migration_audit_writer;
-- GRANT SELECT ON ALL TABLES IN SCHEMA migration_audit TO migration_audit_reader;

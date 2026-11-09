-- Rollback audit_log (RPT-14). Re-create retired access_events table from 0004_share_access.sql.
CREATE TABLE IF NOT EXISTS access_events (
    event_id uuid PRIMARY KEY,
    request_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL CHECK (actor IN ('visitor', 'admin', 'anonymous')),
    session_id uuid,
    link_id uuid,
    action text NOT NULL,
    status_code integer NOT NULL,
    run_id uuid
);

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
DROP FUNCTION IF EXISTS audit_log_prevent_mutation();
DROP TABLE IF EXISTS audit_log CASCADE;

CREATE TABLE feedback (
    feedback_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL,
    text text NOT NULL,
    category text,
    name text,
    screenshot_sha256 text,
    session_id uuid,
    link_id uuid,
    screen text,
    browser text,
    incident_id text,
    run_id uuid,
    report_version text,
    request_id uuid
);

CREATE INDEX idx_feedback_created_at ON feedback (created_at DESC);
CREATE INDEX idx_feedback_run_id ON feedback (run_id) WHERE run_id IS NOT NULL;

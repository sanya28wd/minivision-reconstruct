-- The run log (spec 001, "Run log contract"; the subset RPT-01 needs).
-- Its undo is 0001_create_runs.down.sql (every migration has an up and a down; see incident_report/db.py).
CREATE TABLE runs (
    run_id           uuid             PRIMARY KEY,
    origin           text             NOT NULL CHECK (origin IN ('app', 'benchmark', 'replay')),
    created_at       timestamptz      NOT NULL,
    input_hash       text             NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    report           jsonb,
    status           text             NOT NULL CHECK (status IN ('succeeded', 'failed')),
    total_latency_ms double precision NOT NULL CHECK (total_latency_ms >= 0),
    error            text,
    CHECK (
        (status = 'succeeded' AND report IS NOT NULL AND error IS NULL)
        OR (status = 'failed' AND report IS NULL AND error IS NOT NULL)
    )
);

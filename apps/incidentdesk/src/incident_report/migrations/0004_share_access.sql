-- ADR 0010 / RPT-09. No raw link or cookie secrets are persisted.
CREATE TABLE share_links (
    link_id uuid PRIMARY KEY,
    secret_hash text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    generation integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE anonymous_sessions (
    session_id uuid PRIMARY KEY,
    link_id uuid NOT NULL REFERENCES share_links(link_id),
    secret_hash text NOT NULL,
    generation integer NOT NULL,
    blocked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    UNIQUE (session_id, link_id)
);
CREATE INDEX anonymous_sessions_link_idx ON anonymous_sessions(link_id);
-- RPT-14 will consume/replace this unchained event sink; no tamper-evidence claim here.
CREATE TABLE access_events (
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
ALTER TABLE runs ADD COLUMN session_id uuid, ADD COLUMN link_id uuid, ADD COLUMN request_id uuid;
ALTER TABLE runs ADD CONSTRAINT runs_access_pair CHECK ((session_id IS NULL) = (link_id IS NULL));

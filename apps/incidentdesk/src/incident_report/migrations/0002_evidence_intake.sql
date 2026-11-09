-- One record per uploaded View, durable even when the later AI Run fails.
-- No runs FK: intake is recorded before a Run exists. Parent hashes live in metadata.
CREATE TABLE evidence_intake (
    run_id uuid NOT NULL,
    view_number integer NOT NULL CHECK (view_number >= 0),
    metadata jsonb NOT NULL,
    PRIMARY KEY (run_id, view_number)
);

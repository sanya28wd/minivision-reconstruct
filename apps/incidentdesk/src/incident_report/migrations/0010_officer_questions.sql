-- ADR 0003, ADR 0006, spec 001 (RPT-20):
-- Store optional officer question/context separately from the AI Run and analysis pipeline.
-- Officer text is never sent to vision models or prompts.
CREATE TABLE officer_questions (
    run_id      uuid        PRIMARY KEY,
    question    text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

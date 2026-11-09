-- Q&A never updates runs/report JSON. No FK: run-log test/maintenance truncation remains independent.
CREATE TABLE report_questions (
    question_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    question text NOT NULL,
    report_sha256 text NOT NULL,
    prompt_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'answered', 'failed')),
    answer jsonb
);
CREATE INDEX report_questions_run_created ON report_questions (run_id, created_at);

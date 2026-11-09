-- RPT-21: Support processing status and current step on runs.
ALTER TABLE runs ADD COLUMN current_step text;

ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN ('succeeded', 'failed', 'processing'));

ALTER TABLE runs DROP CONSTRAINT runs_check;
ALTER TABLE runs ADD CONSTRAINT runs_check CHECK (
    (status = 'succeeded' AND report IS NOT NULL AND error IS NULL)
    OR (status = 'failed' AND report IS NULL AND error IS NOT NULL)
    OR (status = 'processing' AND report IS NULL AND error IS NULL)
);

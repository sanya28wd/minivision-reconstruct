DELETE FROM runs WHERE status = 'processing';

ALTER TABLE runs DROP CONSTRAINT runs_check;
ALTER TABLE runs ADD CONSTRAINT runs_check CHECK (
    (status = 'succeeded' AND report IS NOT NULL AND error IS NULL)
    OR (status = 'failed' AND report IS NULL AND error IS NOT NULL)
);

ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN ('succeeded', 'failed'));

ALTER TABLE runs DROP COLUMN current_step;

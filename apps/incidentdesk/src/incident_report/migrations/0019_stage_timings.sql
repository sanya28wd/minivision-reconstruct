ALTER TABLE runs ADD COLUMN stage_timings_ms jsonb NOT NULL DEFAULT '{}'::jsonb;

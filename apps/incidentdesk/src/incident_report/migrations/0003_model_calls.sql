-- RPT-07: retain successful and failed attempts, including malformed model output.
-- Reversible via 0003_model_calls.down.sql; rollback discards these records (back up first).
ALTER TABLE runs ADD COLUMN calls jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE runs ADD COLUMN retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0);

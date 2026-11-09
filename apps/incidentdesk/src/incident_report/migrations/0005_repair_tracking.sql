-- RPT-11: Track fact repair attempts and outcomes on each Run.
-- Reversible via 0005_repair_tracking.down.sql; rollback discards these columns.
ALTER TABLE runs ADD COLUMN repair_attempts integer NOT NULL DEFAULT 0 CHECK (repair_attempts >= 0);
ALTER TABLE runs ADD COLUMN repair_outcome text;

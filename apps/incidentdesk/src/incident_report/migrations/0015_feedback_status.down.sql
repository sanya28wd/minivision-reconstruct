DROP INDEX IF EXISTS idx_feedback_status;
ALTER TABLE feedback DROP COLUMN IF EXISTS status;

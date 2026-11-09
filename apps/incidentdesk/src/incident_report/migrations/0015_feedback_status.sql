ALTER TABLE feedback ADD COLUMN status text NOT NULL DEFAULT 'new';
CREATE INDEX idx_feedback_status ON feedback (status);

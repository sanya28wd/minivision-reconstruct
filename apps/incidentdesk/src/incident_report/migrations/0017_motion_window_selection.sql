-- EXP-MOTION-WINDOWS: experiment diagnostics belong to the Run, separate from Report findings.
ALTER TABLE runs ADD COLUMN motion_window_selection jsonb;

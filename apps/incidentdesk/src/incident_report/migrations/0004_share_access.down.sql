-- Rollback destroys access records; take a backup first (RPT-03).
ALTER TABLE runs DROP CONSTRAINT runs_access_pair;
ALTER TABLE runs DROP COLUMN request_id, DROP COLUMN link_id, DROP COLUMN session_id;
DROP TABLE access_events;
DROP TABLE anonymous_sessions;
DROP TABLE share_links;

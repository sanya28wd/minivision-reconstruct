-- RPT-38 (spec 001: Override, Approve, lock, versions). Officer decisions on a Report.
-- Three append-only tables, protected the way audit_log is (migration 0006): row triggers block UPDATE and
-- DELETE, statement triggers block TRUNCATE, and PUBLIC loses UPDATE/DELETE/TRUNCATE.
-- There is deliberately no foreign key to runs: tests (and the benchmark) truncate runs, and an FK would
-- either block that or cascade into these tables. run_id is checked by the application instead.
-- Version 1 of every Report is implicit (it is the AI's original, nothing was decided yet); a row in
-- report_versions exists only for version 2 and later. The AI's report is never copied or changed.

-- One row per version >= 2. parent_version is the approved version it branched from, so a version's
-- effective overrides are its own plus those inherited from its parents (nothing is ever copied or edited).
CREATE TABLE report_versions (
    run_id         uuid        NOT NULL,
    version_number integer     NOT NULL CHECK (version_number >= 2),
    parent_version integer     NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    session_id     uuid,
    link_id        uuid,
    request_id     uuid,
    PRIMARY KEY (run_id, version_number),
    CHECK (parent_version = version_number - 1)
);

-- One row per Override. A second Override of the same finding in the same version is a new row; the newest
-- row wins and the older one stays on record. ai_original is the AI's own finding, copied by the server.
CREATE TABLE report_overrides (
    override_seq   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    override_id    uuid        NOT NULL UNIQUE,
    run_id         uuid        NOT NULL,
    version_number integer     NOT NULL CHECK (version_number >= 1),
    finding_key    text        NOT NULL CHECK (finding_key ~ '^(party|violation):[0-9]{1,4}$'),
    ai_original    jsonb       NOT NULL,
    new_value      text        NOT NULL CHECK (length(btrim(new_value)) > 0),
    reason_code    text        NOT NULL CHECK (reason_code IN
                       ('video_shows_otherwise', 'wrong_party', 'wrong_timestamp', 'other')),
    reason_text    text        NOT NULL CHECK (length(btrim(reason_text)) > 0),
    created_at     timestamptz NOT NULL DEFAULT now(),
    session_id     uuid,
    link_id        uuid,
    request_id     uuid
);
CREATE INDEX report_overrides_run_idx ON report_overrides (run_id, version_number, override_seq);

-- One row per Approve. A version with a row here is locked.
CREATE TABLE report_approvals (
    run_id           uuid        NOT NULL,
    version_number   integer     NOT NULL CHECK (version_number >= 1),
    approved_at      timestamptz NOT NULL DEFAULT now(),
    overrides_digest text        NOT NULL CHECK (overrides_digest ~ '^[0-9a-f]{64}$'),
    session_id       uuid,
    link_id          uuid,
    request_id       uuid,
    PRIMARY KEY (run_id, version_number)
);

CREATE OR REPLACE FUNCTION report_decisions_prevent_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'report decision rows are immutable: % operations are forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER report_versions_no_update_delete
BEFORE UPDATE OR DELETE ON report_versions
FOR EACH ROW EXECUTE FUNCTION report_decisions_prevent_mutation();
CREATE TRIGGER report_versions_no_truncate
BEFORE TRUNCATE ON report_versions
FOR EACH STATEMENT EXECUTE FUNCTION report_decisions_prevent_mutation();

CREATE TRIGGER report_overrides_no_update_delete
BEFORE UPDATE OR DELETE ON report_overrides
FOR EACH ROW EXECUTE FUNCTION report_decisions_prevent_mutation();
CREATE TRIGGER report_overrides_no_truncate
BEFORE TRUNCATE ON report_overrides
FOR EACH STATEMENT EXECUTE FUNCTION report_decisions_prevent_mutation();

CREATE TRIGGER report_approvals_no_update_delete
BEFORE UPDATE OR DELETE ON report_approvals
FOR EACH ROW EXECUTE FUNCTION report_decisions_prevent_mutation();
CREATE TRIGGER report_approvals_no_truncate
BEFORE TRUNCATE ON report_approvals
FOR EACH STATEMENT EXECUTE FUNCTION report_decisions_prevent_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON report_versions, report_overrides, report_approvals FROM PUBLIC;

-- The lock lives in the database too, not only in the application: an Override cannot be added to an
-- approved version, and a new version can only branch from an approved one.
CREATE OR REPLACE FUNCTION report_overrides_require_unlocked()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM report_approvals a
               WHERE a.run_id = NEW.run_id AND a.version_number = NEW.version_number) THEN
        RAISE EXCEPTION 'report version % of run % is approved and locked', NEW.version_number, NEW.run_id;
    END IF;
    IF NEW.version_number > 1 AND NOT EXISTS (SELECT 1 FROM report_versions v
               WHERE v.run_id = NEW.run_id AND v.version_number = NEW.version_number) THEN
        RAISE EXCEPTION 'report version % of run % does not exist', NEW.version_number, NEW.run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER report_overrides_unlocked_only
BEFORE INSERT ON report_overrides
FOR EACH ROW EXECUTE FUNCTION report_overrides_require_unlocked();

CREATE OR REPLACE FUNCTION report_versions_require_approved_parent()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM report_approvals a
                   WHERE a.run_id = NEW.run_id AND a.version_number = NEW.parent_version) THEN
        RAISE EXCEPTION 'a new report version can only branch from an approved version';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER report_versions_approved_parent_only
BEFORE INSERT ON report_versions
FOR EACH ROW EXECUTE FUNCTION report_versions_require_approved_parent();

CREATE OR REPLACE FUNCTION report_approvals_require_version()
RETURNS trigger AS $$
BEGIN
    IF NEW.version_number > 1 AND NOT EXISTS (SELECT 1 FROM report_versions v
               WHERE v.run_id = NEW.run_id AND v.version_number = NEW.version_number) THEN
        RAISE EXCEPTION 'report version % of run % does not exist', NEW.version_number, NEW.run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER report_approvals_existing_version_only
BEFORE INSERT ON report_approvals
FOR EACH ROW EXECUTE FUNCTION report_approvals_require_version();

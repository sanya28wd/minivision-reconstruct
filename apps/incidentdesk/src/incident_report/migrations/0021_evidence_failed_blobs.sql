-- R2 additive store for bytes collected before a read failure. 0020 remains unchanged.
CREATE TABLE evidence_import_failed_blobs (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    content bytea NOT NULL
);
CREATE TABLE evidence_import_failed_artifacts (
    import_id text NOT NULL REFERENCES evidence_imports(import_id),
    artifact_path text NOT NULL,
    sha256 text NOT NULL REFERENCES evidence_import_failed_blobs(sha256),
    PRIMARY KEY (import_id, artifact_path)
);
CREATE TRIGGER failed_blobs_no_update_delete BEFORE UPDATE OR DELETE ON evidence_import_failed_blobs
FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER failed_blobs_no_truncate BEFORE TRUNCATE ON evidence_import_failed_blobs
FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER failed_artifacts_no_update_delete BEFORE UPDATE OR DELETE ON evidence_import_failed_artifacts
FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER failed_artifacts_no_truncate BEFORE TRUNCATE ON evidence_import_failed_artifacts
FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON evidence_import_failed_blobs, evidence_import_failed_artifacts FROM PUBLIC;

-- MIG-02: additive append-only imports. Original artifacts remain byte-exact.
CREATE TABLE evidence_imports (
    import_id text PRIMARY KEY CHECK (import_id ~ '^[a-f0-9]{64}$'),
    importer_version text NOT NULL,
    provenance_state text NOT NULL CHECK (provenance_state IN ('verified','incomplete_provenance','rejected')),
    decision jsonb NOT NULL,
    raw_envelope bytea,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence_import_records (
    import_id text NOT NULL REFERENCES evidence_imports(import_id),
    record_id text NOT NULL,
    kind text NOT NULL,
    view_id text NOT NULL,
    record jsonb NOT NULL,
    PRIMARY KEY (import_id, record_id)
);
CREATE TABLE evidence_import_artifacts (
    import_id text NOT NULL REFERENCES evidence_imports(import_id),
    artifact_path text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    content bytea NOT NULL,
    PRIMARY KEY (import_id, artifact_path)
);
CREATE FUNCTION evidence_import_prevent_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'evidence imports are immutable: % operations are forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER evidence_imports_no_update_delete BEFORE UPDATE OR DELETE ON evidence_imports
FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER evidence_imports_no_truncate BEFORE TRUNCATE ON evidence_imports
FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER evidence_import_records_no_update_delete BEFORE UPDATE OR DELETE ON evidence_import_records
FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER evidence_import_records_no_truncate BEFORE TRUNCATE ON evidence_import_records
FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER evidence_import_artifacts_no_update_delete BEFORE UPDATE OR DELETE ON evidence_import_artifacts
FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER evidence_import_artifacts_no_truncate BEFORE TRUNCATE ON evidence_import_artifacts
FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON evidence_imports, evidence_import_records, evidence_import_artifacts FROM PUBLIC;

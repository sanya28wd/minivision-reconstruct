-- MIG-03 append-only workspace records. Report/AI/fault tables remain unchanged.
-- Like 0016, incident existence is validated under a runs row lock in the app.
-- No runs FK: the benchmark/test reset truncates runs without deleting audit history.
CREATE TABLE incident_evidence_imports (
 link_id uuid PRIMARY KEY,
 incident_id uuid NOT NULL,
 import_id text NOT NULL REFERENCES evidence_imports(import_id),
 view_sha256 text NOT NULL CHECK (view_sha256 ~ '^[a-f0-9]{64}$'),
 source_view_id text NOT NULL,
 producer_case_id text NOT NULL,
 attached_at timestamptz NOT NULL DEFAULT now(),
 attached_by text NOT NULL,
 -- Supersession is recorded only in incident_evidence_supersessions (append-only).
 sequence_number bigint GENERATED ALWAYS AS IDENTITY,
 UNIQUE (incident_id,import_id,view_sha256),
 UNIQUE (incident_id,link_id)
);
CREATE TABLE incident_evidence_supersessions (
 incident_id uuid NOT NULL,
 old_link_id uuid PRIMARY KEY,
 new_link_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY (incident_id,old_link_id) REFERENCES incident_evidence_imports(incident_id,link_id),
 FOREIGN KEY (incident_id,new_link_id) REFERENCES incident_evidence_imports(incident_id,link_id),
 CHECK (old_link_id <> new_link_id)
);
CREATE TABLE track_party_mappings (
 mapping_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 incident_id uuid NOT NULL,
 import_id text NOT NULL REFERENCES evidence_imports(import_id),
 track_ref text NOT NULL,
 view_sha256 text NOT NULL,
 party_code text,
 action text NOT NULL CHECK (action IN ('map','unmap','cannot_determine')),
 reviewer text NOT NULL,
 reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((action='map' AND party_code IS NOT NULL) OR (action<>'map' AND party_code IS NULL))
);
CREATE TABLE view_time_alignments (
 alignment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 incident_id uuid NOT NULL,
 view_sha256 text NOT NULL,
 reference_view_sha256 text NOT NULL,
 offset_seconds double precision NOT NULL CHECK (offset_seconds > '-Infinity'::float8 AND offset_seconds < 'Infinity'::float8),
 uncertainty_seconds double precision NOT NULL CHECK (uncertainty_seconds >= 0 AND uncertainty_seconds < 'Infinity'::float8),
 clock_reliability text NOT NULL DEFAULT 'unknown' CHECK (clock_reliability IN ('unknown','reviewer_estimated','shared_clock_documented')),
 reviewer text NOT NULL,
 reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER incident_evidence_imports_no_update_delete BEFORE UPDATE OR DELETE ON incident_evidence_imports FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER incident_evidence_imports_no_truncate BEFORE TRUNCATE ON incident_evidence_imports FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON incident_evidence_imports FROM PUBLIC;
CREATE TRIGGER incident_evidence_supersessions_no_update_delete BEFORE UPDATE OR DELETE ON incident_evidence_supersessions FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER incident_evidence_supersessions_no_truncate BEFORE TRUNCATE ON incident_evidence_supersessions FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON incident_evidence_supersessions FROM PUBLIC;
CREATE TRIGGER track_party_mappings_no_update_delete BEFORE UPDATE OR DELETE ON track_party_mappings FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER track_party_mappings_no_truncate BEFORE TRUNCATE ON track_party_mappings FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON track_party_mappings FROM PUBLIC;
CREATE TRIGGER view_time_alignments_no_update_delete BEFORE UPDATE OR DELETE ON view_time_alignments FOR EACH ROW EXECUTE FUNCTION evidence_import_prevent_mutation();
CREATE TRIGGER view_time_alignments_no_truncate BEFORE TRUNCATE ON view_time_alignments FOR EACH STATEMENT EXECUTE FUNCTION evidence_import_prevent_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON view_time_alignments FROM PUBLIC;

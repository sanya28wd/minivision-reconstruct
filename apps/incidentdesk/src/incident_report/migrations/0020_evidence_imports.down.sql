-- Destructive rollback on owner-controlled disposable data only; migration runner is transactional.
DROP TABLE evidence_import_records;
DROP TABLE evidence_import_artifacts;
DROP TABLE evidence_imports;
DROP FUNCTION evidence_import_prevent_mutation();

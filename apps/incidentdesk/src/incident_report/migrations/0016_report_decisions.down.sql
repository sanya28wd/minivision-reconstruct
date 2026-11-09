-- Rollback of RPT-38. WARNING: this deletes every recorded Override, version and approval.
-- The audit_log rows that describe them stay (audit_log is not touched here) but the rows they point to are gone.
-- Dropping a table does not fire its row or TRUNCATE triggers, so the immutability triggers do not block this.
DROP TABLE IF EXISTS report_approvals;
DROP TABLE IF EXISTS report_overrides;
DROP TABLE IF EXISTS report_versions;
DROP FUNCTION IF EXISTS report_approvals_require_version();
DROP FUNCTION IF EXISTS report_versions_require_approved_parent();
DROP FUNCTION IF EXISTS report_overrides_require_unlocked();
DROP FUNCTION IF EXISTS report_decisions_prevent_mutation();

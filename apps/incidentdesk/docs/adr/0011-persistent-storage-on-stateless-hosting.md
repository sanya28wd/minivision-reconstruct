---
status: accepted
date: 2026-10-12
---

# Evidence lives in Cloud Storage and records in managed PostgreSQL, never on the app server's disk

The app runs on Google Cloud Run. Its container filesystem is **in-memory and does not persist when an instance stops**, and writing large files there consumes instance memory (Cloud Run container contract, read 2026-10-12). Cloud Storage FUSE mounts provide **no file locking**, so a SQLite file there is unsafe (Cloud Run storage-mount docs). The earlier plan (SQLite plus local files on Cloud Run) would have lost the run log, the audit trail and the evidence on the first restart.

**Decision:**
- **Original videos, working copies and Feedback screenshots** go to **Cloud Storage**. Originals are write-once, keyed by SHA-256, and stored with object-retention/versioning where available.
- **Incidents, runs, findings, overrides, report versions, Feedback, sessions and the hash-chained audit log** go to **Cloud SQL for PostgreSQL** (smallest tier, paid from the trial credit). Automated backups and point-in-time recovery are on, and a **restore is tested**. Audit immutability is enforced with database triggers and permissions.
- **Every environment uses PostgreSQL**, including CI (a service container) and local development, so triggers and queries behave the same everywhere.
- **Separate environments:** dev, staging and production use separate projects or instances and separate credentials. Agents never hold staging or production credentials. The owner promotes to production manually.

Verify current Cloud SQL pricing and trial-credit coverage at implementation, alongside the Vertex billing check (ADR 0009). If cost rules out Cloud SQL, the fallback is a single small VM with a persistent disk running PostgreSQL, with the same backup and restore requirement.

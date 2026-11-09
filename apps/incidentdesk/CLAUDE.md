# CLAUDE.md: IncidentDesk report app

The officer-facing report app (package `incident_report`), imported from `josephjose-dev/RTA-ACCIDENT-AGENT` at `347a778` (see `docs/migration/IMPORT_LOG.md` at the repo root). Its evaluation code, benchmark truth and saved results stay in that donor repo, which runs them.

## Hard guardrails
- **Traceability (`docs/adr/0006`):** every finding cites view and source timestamp. Every fine, black point, impound period or article cites its official source and item number (MR 178/2017 as amended; Decree-Law 14/2024). Secondary-only figures are provisional; missing evidence becomes **Cannot Determine**.
- **RTA-native (`docs/adr/0004`):** Dubai Police pink/green/white fault status and official categories only. No government logos or branding.
- **Pilot shows fault and fines** (owner 2026-10-30), behind those two rules. Driver-behaviour violations (phone, seatbelt) list their cited fine and leave collision Fault Status unchanged.
- **Imported reconstruction evidence** enters as proposals with provenance; fault rules read reviewed facts only.
- **Safety:** officer and Feedback text stays isolated from vision prompts; all model and user text is escaped when rendered; Gemini schema limits are respected.
- **Credentials:** agents hold no staging or production credentials. `.env` (API keys) and `.env.test` (local test DB) are created by the owner and stay gitignored. Replay and tests need no API key.
- **Claims:** an accuracy or latency claim carries the exact command, corpus, configuration and raw result.
- **Clean-room:** REA/AnalyzerPro material informs logic only; its source code is never copied.

## Checks
`bash scripts/check_all.sh` (or `scripts/check_all.ps1`) from this folder is the gate: format, lint, mypy strict, pytest, guards. Tests and guards change only with owner approval; protection may widen, never narrow. Database: `incidentdesk_dev`, separate from the donor's.

## Reference
Reviewer judgement rules: `CODING_STANDARDS.md`. Domain terms: `GLOSSARY.md` (protected). Approved scope: `specs/001-incident-video-report/spec.md`; the shared MVP's differences are recorded in `docs/migration/`, not edited into the spec.

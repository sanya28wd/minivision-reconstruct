# MIG-03 work log

Part A L2 — Tell the collaborator: producer_case_id remains an optional binding in
our envelope, identifying each unchanged producer case inside a shared application
case. Her manifests, CLI flags, schemas and workflows require no edits. Contract
README already documents the field and its single-view fallback.

## Implementation and boundaries

Branch `mig-03-timeline` starts at accepted integration `e825edb`. The application
workspace is behind `INCIDENTDESK_RECONSTRUCTION=true` (off by default), with local
`INCIDENTDESK_EVIDENCE_EXPORTS_ROOT` pointing at export bundles. There is no model
call and no reconstruction input to rules or AI. Report data and the PDF endpoint
remain unchanged. Browser printing hides the proposal workspace.

A preserves sequence intervals and covering frames and emits structured diagnostic
codes. B attaches immutable imports by the report view's original video SHA-256;
new revisions append supersession edges. C merges current proposals with app cited
moments on each view's native clock. Reviewer confirmation is explicitly a
reconstruction review, never officer approval. Rejected proposals are struck through;
off-table corrections never reuse an old citation.

E stores explicit same-view track associations, defaults to unmapped and keeps stale
history. Re-confirmation writes a new row on the current import/ref. Producer class
is carried in importer 1.4.0; old snapshots without it remain readable as unknown.
F stores reviewer offsets, required uncertainty and reliability (`unknown`,
`reviewer_estimated`, `shared_clock_documented`). Unknown reliability never creates
cross-camera order. Views aligned to a common reference are grouped by overlapping
uncertainty intervals; citations retain native times. Unresolved producer-order
groups stay in their per-view timeline.

Migration 0022 has up/down files and four append-only histories. After a disposable
test application exposed reset incompatibility, the unmerged migration was corrected
to follow 0016: validate incident existence under a row lock in the application,
without a `runs` FK. This preserves the existing benchmark/test `TRUNCATE runs`
convention without cascading into audit history. No historical migration, guard,
donor-imported test or collaborator path was changed.

## Local operation

Windows application (existing app environment and local database configuration):

```powershell
$env:INCIDENTDESK_RECONSTRUCTION='true'
$env:INCIDENTDESK_EVIDENCE_EXPORTS_ROOT='D:/minivision-incidentdesk/exports'
# From apps/incidentdesk, use the existing app startup command on 127.0.0.1:8000.
.venv/Scripts/python -m incident_report.evidence_import attach --incident <run-uuid> --attached-by 'Reviewer' relative-bundle
```

WSL, from this checkout:

```sh
export INCIDENTDESK_RECONSTRUCTION=true
export INCIDENTDESK_CASES_ROOT=/mnt/d/minivision-incidentdesk/cases
export RECONSTRUCTION_PYTHON=/mnt/d/minivision-incidentdesk/.venv-linux/bin/python
export RECONSTRUCTION_FFMPEG=/mnt/d/minivision-incidentdesk/linux-tools/ffmpeg/bin/ffmpeg
sh integration/launch_reconstruction_tools.sh
```

The launcher supplies only the unchanged entry points, `--cases-root`, replay's
`--media-port 8766 --ffmpeg`, and Streamlit server/browser flags. Streamlit binds to
127.0.0.1 on 8511/8512/8513. Usage statistics are disabled. No browser path reaches
these processes. The UI shows exact dropdown selections with a copy button; no
deep-link parameter exists in the collaborator tools.

Caddy is a separate dev binary, absent from dependency/lock files. This machine has
no winget command, so the official release was downloaded outside the repository:

```powershell
gh release download v2.11.7 --repo caddyserver/caddy --pattern caddy_2.11.7_windows_amd64.zip --pattern caddy_2.11.7_checksums.txt --dir D:/minivision-incidentdesk/dev-tools/caddy
# Compare archive SHA512 with the matching entry in the published checksums file.
Expand-Archive D:/minivision-incidentdesk/dev-tools/caddy/caddy_2.11.7_windows_amd64.zip D:/minivision-incidentdesk/dev-tools/caddy
D:/minivision-incidentdesk/dev-tools/caddy/caddy.exe validate --config integration/proxy/Caddyfile --adapter caddyfile
D:/minivision-incidentdesk/dev-tools/caddy/caddy.exe run --config integration/proxy/Caddyfile --adapter caddyfile
```

Version: `v2.11.7 h1:yj0Y4fYZGPkSvibBJ1sTWE33xC0fxztVyXEW5iIdUT4=`.
Browser address: `http://127.0.0.1:8080`. Without Caddy, the app's fixed tool routes
return a visible 503 explanation; Caddy also reports offline backends visibly.
These are local development processes, not a deployment/authentication boundary.

## Historical Part D blockers (R1; disposition superseded by R2 below)

All three routes passed HTTP 200, health 200 and WebSocket 101 plus ping/pong
through Caddy. These transport checks do not prove manual UI correctness.

Computer Use stopped before browser inspection because it could not determine
Chrome's current URL confidently enough to enforce policy. No screenshots or saved
track-edit smoke are claimed. UI actions stopped; no alternative desktop automation
was used to bypass the block.

The replay producer hardcodes absolute media URLs at
`http://127.0.0.1:<media-port>/<token>` in `reconstruction/media.py`. Its existing flags
cannot replace that URL. Caddy exposes `/reconstruct/media/`, but the unchanged
browser component still requests port 8766 directly. Thus replay is linked out and
the full single-address requirement is not achieved. Chromium is needed for replay
exports per README; it was not installed. No producer code or renderer was patched.

Alternative found: the standard Streamlit baseUrlPath flag successfully routes the
tool shells and WebSockets without changing their code. It does not rewrite the
producer's media URLs. Owner/collaborator resolution is needed for that remaining
seam; an in-app Python/WebSocket proxy was not introduced.

## Tell the collaborator

- Keep producing existing case IDs, manifests and revisions; `producer_case_id`
  belongs to our envelope binding and does not require a producer change.
- We launch the three existing scripts with their existing cases-root/media/ffmpeg
  flags and Streamlit's baseUrlPath/server flags. Cases remain chosen by dropdown.
- Imported review confirmation remains a proposal review. Track associations and
  clock estimates are reviewer-set display aids, absent from fault/AI inputs.
- The absolute replay media URL prevents complete single-address routing with
  existing flags. No change to her code has been made or implicitly requested.

Full gate logs, fixture counts, focused commands and independent review findings
are in the donor `.agent_bridge/results/MIG-03-TIMELINE-WORKSPACE.md`. Independent
Claude critical acceptance remains required; no acceptance, merge, push or tag.

## Round 2: stage 1 and smoke checklist

The 2026-11-05 Round 2 card accepts replay media on loopback port 8766 for this
local, single-machine stage. Caddy routes the three tool shells and WebSockets.
Full same-address media is deferred to stage 2 / remote access. No producer code
change is needed or authorized. Claude performs and records the browser smoke;
the seed and automated checks below are preparation, not manual acceptance evidence.

The seed helper writes only to `incidentdesk_dev` with `INCIDENT_ENV=dev` and a
loopback-pinned PostgreSQL endpoint. It refuses other database names, nonlocal
endpoints and the configured `TEST_DATABASE_URL` (including loopback aliases).
It never migrates/resets a DB, creates a share link, edits producer cases, or calls AI.
It copies the selected fixture bytes to a fresh child of the exports root, stores
the hash-verified original video, and records one succeeded synthetic incident
with a cited navigation marker and an explicitly synthetic party for mapping.
Every invocation creates a new incident/export; reuse the IDs in the result rather
than rerunning for each browser attempt. Partial failed seeds are left for inspection,
never automatically deleted or overwritten.

1. Start `incident-pg` on 5433. In Windows, configure `DATABASE_URL` for the local
   `incidentdesk_dev` database, `INCIDENT_ENV=dev`, and `INCIDENTDESK_RECONSTRUCTION=true`.
   Create/migrate the **development** DB if needed with the existing DB workflow
   (`python -m incident_report.db migrate` from `apps/incidentdesk`). Do not point
   the helper at the admin/test DB. Configure existing local directories for
   `INCIDENTDESK_EVIDENCE_EXPORTS_ROOT` and `INCIDENT_EVIDENCE_DIR`, outside the repo.
   Use the same values for the helper and app. The exact prepared incident and
   machine paths are in the donor result's Round 2 section.
2. Reuse the prepared seed, or from the repo root run the following with the app's
   Python and the unchanged original from the shared cases folder:

   ```powershell
   apps/incidentdesk/.venv/Scripts/python integration/smoke/seed_smoke_incident.py --fixture mig02-multi-review --video '<local-cases-root>/mig02-multi-review/original/mig02-multi-review-staged-synthetic.avi' --exports-root $env:INCIDENTDESK_EVIDENCE_EXPORTS_ROOT --evidence-root $env:INCIDENT_EVIDENCE_DIR
   ```

   Retain the printed `run_id`, `review_path`, `bundle`, video SHA-256 and producer
   case. A mismatched original refuses before file/DB writes. Synthetic labels
   must stay visible; this is a UI fixture, not footage analysis or a fault result.
3. From `apps/incidentdesk`, start the app using the same configuration:
   `.venv/Scripts/python -m uvicorn --factory incident_report.web:create_app --host 127.0.0.1 --port 8000`.
   From the repo root start the existing Caddy dev binary:
   `caddy run --config integration/proxy/Caddyfile --adapter caddyfile`.
   In WSL set the shared cases root, Python and ffmpeg paths from `CLAUDE.local.md`,
   enable the feature, and run `sh integration/launch_reconstruction_tools.sh`.
   Tool ports are 8511/8512/8513; replay media stays 8766. Keep all listeners local.
4. At `http://127.0.0.1:8080`, open an existing valid local share session and the
   printed review path. Select the printed bundle, enter reviewer attribution,
   and attach. Verify the synthetic marker and reconstruction events share the
   per-view timeline; no separate report/PDF content or fault change is introduced.
5. Check proposal origin and reconstruction-review badges, native view/frame/time
   citations, corrected/original time display, and unknown-order labels. Record a
   screenshot with the attached case/run/revision selection details visible.
6. Check a rejected event is struck through and never confirmed-styled. **The six
   unchanged MIG-02 fixtures have no rejected event row.** To exercise this manually,
   use the producer's existing review/export workflow on a disposable smoke case
   and attach its genuinely rejected revision; do not edit fixture JSON or fabricate
   a review. Until such an export is supplied, record this step as not exercised.
   The automated synthetic rejection test remains evidence only for rendering.
7. Click an event and verify the existing source player's time moves to its native
   source time. The helper preserves AVI originals; browser codec support must be
   verified. If playback fails, record that failure and the browser/codec; do not
   rename/transcode bytes under the fixture hash or claim the seek smoke passed.
   A supported original needs a matching producer export, not a substituted hash.
8. Map a track explicitly to the synthetic same-view white Toyota sedan, with a
   reviewer/reason. Reload to verify its descriptive label. Exercise cannot-determine
   and unmap if needed; no automatic identity or fault inference should appear.
9. Set a reviewer alignment with required uncertainty (for this single-view seed,
   reference the same camera with zero offset). Reload and check the saved record;
   one camera must not acquire a cross-camera order. For combined order smoke use
   a genuine two-view incident and record both cameras against one reference.
10. Open `/reconstruct/tracks/`, `/reconstruct/calibration/` and
    `/reconstruct/replay/` through Caddy. In each dropdown select the exact case and
    run/revision displayed beside the app links; no URL deep-link is implied.
    Record one screenshot per tool and verify its WebSocket connection in browser
    developer tools. Save one track edit on a disposable **development case**, then
    verify its new saved revision. Calibration changes are unnecessary for smoke.
11. Open reconstruction replay; verify media requests to `127.0.0.1:8766` work.
    Do not install Chromium/Playwright for producer exports silently. If a corrected
    export is available, re-import it into this same incident and confirm old history
    remains readable and prior associations need re-confirmation. Record every
    unexercised/failing step, then obtain Claude's independent critical review.

Round 2 LOW carry-overs: malformed windows quarantine a row, old typed snapshots
remain readable, superseded reattachments show their successor import, same-view
moments keep native order, and clip-wide track states say "at some point in this
clip". The unavailable notice is print-excluded. Supersession uses its append-only
edge table only; the unused 0022 link column was removed before merge. L8 GET cost/
locking and L11 mapping composite FK remain explicitly deferred by the card.

# UI stage 2 — Claude browser smoke

Use disposable development cases/reports and the existing share-link workflow.
Do not use real/NDA footage, model calls, production credentials or held-out truth.
Record the branch/commit, app/worker configuration, report/attachment IDs, source
case/run/revisions, screenshots and exact files checked. Preserve failed steps.

1. Start the app from this worktree's own app environment/database configuration.
   Start its WSL worker with the documented local settings and existing collaborator
   environment. Enable reconstruction and native-tools flags. Confirm worker health
   with a token-bearing server-side request; never put the token in the browser.
2. Attach a genuine unchanged MIG-02 bundle to its matching synthetic video, using
   the existing smoke seed/attach flow. Confirm tool links contain report/attachment
   IDs and open native pages. No case filesystem path may appear as a browser input.
3. Track review: load an observed frame and one with unavailable/lost/interpolated
   evidence. Verify states/provenance and image boxes. Save a box-only correction:
   class must stay unchanged. Exercise class correction, create/add, observation/
   whole-track delete, merge, split, repair, undo and reviewed-track export on copies.
   Require reviewer/reason. Compare saved events/review files with the corresponding
   original workflow actions, pinning generated IDs/time for byte comparison.
4. Calibration: click a point and complete its world coordinates; perform the same
   action through the keyboard point list. Edit control/held-out lists, units,
   tolerance and all four feature kinds. Preview and download. Save once and verify
   the new revision remains selected at the same source frame. Delete a feature,
   add another and verify IDs remain unique. Compare JSON and road-overlay PNG
   bytes against a direct producer save. Invalid mode/kind/validation must not save.
5. Replay: verify the unchanged workspace loads both views, playback, exact-frame
   navigation, pan/zoom, track selection and marker tools. Inspect browser network:
   assets, bundle, frames and byte-range video must use the app origin. No browser
   request may target the worker or port 8766. Record one screenshot and a 206 range
   response. All required mount fields must be supplied without changing her JS.
6. Edit a scene and marker before saving either. Seek from an incident candidate;
   neither edit may disappear. Save one editor; the other's pending changes must
   survive. Save both; verify revision files and seals against direct public saves.
   Exercise snapshot download and saved-revision reconstruction export. Check
   unsupported/missing media are visible failures, never substituted evidence.
7. Inspect incident candidates/assessments and motion tables: native frames/time,
   source windows, confidence, support/counterevidence, decisive unresolved facts,
   non-metric units and unresolved ordering must remain visible. Confirm/reject/
   modify an event and assessment, including clearing every contributor deliberately.
   Historical review revisions may be viewed/exported but must refuse new decisions.
   Export reviewed view; compare revision/export bytes with original workflow output.
8. Export reviewed view and attach. Verify the generated bundle is imported by the
   existing importer, the old attachment stays in history and mappings require
   re-confirmation. Open the incident using the completion link. If export succeeds
   but attachment fails, verify the UI identifies the existing bundle and provides
   recovery guidance instead of a blind retry. The old native selection is retired
   after a successful attachment.
9. Refuse missing share session, bad CSRF, cross-origin writes, missing attribution,
   forged attachment/case/run/revision selections and linked cases. Reviewer/producer
   text must render as text. Audit hashes/session attribution must exist. Under a
   forced post-save audit failure, completion must be explicit and retry discouraged.
10. Disable the native-tools flag. Legacy Caddy/Streamlit links and standalone tools
    remain available; native routes refuse access. Compare print HTML bytes with the
    flag on/off for the same report/session. Fault Status, rules, fines, storyline,
    summary, Q&A and PDF content must remain unaffected.

Not executed by Codex as a manual browser smoke. Automated parity/HTTP checks and
known original-suite failures are recorded separately in the donor result; Claude
records browser outcomes and independent acceptance before merge. MIG-05 merges
first; this branch may then need rebase/review. No push/tag/merge is authorized here.

## Real-browser regression check (R4-3, 5 lines)

Automated: `node --test integration/tools_worker/test_native_forms.mjs` runs the real page scripts against a DOM shim
that copies the browser's form-property shadowing. Manual, in Chromium with DevTools Network open, on the track page and
the calibration page: (1) press Save; (2) the POST goes to `/reports/<run>/tools/<link>/tracks|calibration`, never to a
URL containing `object`; (3) no "Illegal invocation" in the console; (4) force a 404 (edit the form `action` attribute in
DevTools) and confirm the page shows "refused (HTTP 404)", hides Retry, and the next Save is allowed; (5) stop the app
for a moment, press Save, and confirm the page keeps Retry and the same action ID.

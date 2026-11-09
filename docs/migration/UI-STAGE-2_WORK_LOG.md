# Native reconstruction tools (UI stage 2)

The app renders track review, calibration and replay pages using its existing
share-session access and CSRF boundaries. The browser uses app-origin URLs only.
The app calls a loopback Starlette worker with standard-library urllib. The worker
runs in the collaborator environment and calls public producer workflow functions.
Producer source, algorithms, saved formats and built replay assets are unchanged.

## Configuration and startup

Keep machine paths in gitignored `CLAUDE.local.md`. Each component keeps its own
locked environment. From `apps/incidentdesk`, install with `uv sync --locked`.
The app needs `INCIDENTDESK_RECONSTRUCTION=true`, `INCIDENTDESK_NATIVE_TOOLS=true`,
`INCIDENTDESK_EVIDENCE_EXPORTS_ROOT`, and `INCIDENTDESK_TOOLS_TOKEN`. Native tools
default off. The same flag off retains the existing Caddy/Streamlit links.

In WSL, configure `RECONSTRUCTION_PYTHON` to the existing collaborator Python,
`INCIDENTDESK_CASES_ROOT`, and the WSL spelling of the same evidence-exports root.
Case/export roots must be existing, separate local directories. Set the same local
token in both process environments, without putting it in a URL or command argument.
Enable `INCIDENTDESK_NATIVE_TOOLS=true`, then from this checkout run:

```sh
sh integration/launch_tools_worker.sh
```

The worker binds only `127.0.0.1:8520`; proxy headers and access logs are disabled.
Its factory rejects missing/invalid configuration and every request needs the token.
There is no CORS. Set `RECONSTRUCTION_FFMPEG` when ffmpeg is not on PATH. Producer
snapshot/video exports also require the already installed Playwright Chromium and
native browser libraries; local cache/library paths belong in `CLAUDE.local.md`.
No detector, tracking pipeline or model call is started by these pages.

The old `integration/launch_reconstruction_tools.sh` and all three Streamlit apps
remain runnable separately. Do not run a new worker on an occupied port or stop
someone else's process.

## Evidence and editing semantics

Native page URLs contain the report and current attachment identifiers. Case/run
selection is recovered from that immutable import; browser paths are never accepted.
Historical superseded links cannot write. Producer manifest paths, hashes and case
links are validated before workflow calls. The local directory owner remains trusted;
this is not an OS sandbox against concurrent malicious filesystem replacement.

All saves require the existing share session, same-origin CSRF, reviewer and reason.
The existing app audit log records session/request/attachment IDs and reviewer/reason
hashes. Raw reason text is not added to the app audit trail. Producer functions keep
their own output format; undo/export/calibration files do not gain new reason fields.
Audit intent is written before calling the worker; a completed mutation with failed
audit confirmation is explicitly reported as completed, with no instruction to retry.
Worker timeouts leave completion uncertain: inspect saved revisions before repeating.

Track boxes use source pixels; added-observation timestamps/dimensions come from
decoded evidence. Box-only corrections preserve class unless explicitly changed.
Calibration exposes clickable and keyboard-editable points plus road features;
its public validator decides whether a revision may be saved. A new calibration
requires a separately prepared reconstruction run before it changes replay geometry.

Replay mounts unchanged `workspace.js`/`workspace.css` via our host adapter. Assets,
bundle, exact frames and range-capable video are served through authenticated app
routes. Scene/marker saves and seeks preserve other pending editor changes. Motion
tables show the chosen source frame; event/sequence tables retain source windows and
ordering uncertainty. Contributor evidence retains support, counterevidence and
decisive unresolved facts. These are proposals, never Fault Status or rule findings.
Incident decisions require the latest review revision; historical views remain
readable/exportable. Replacing contributors with an explicitly empty list is supported.

Replay writes retain the last 256 action IDs per worker process (project assumption),
like the original process/session lifetime. Deduplication is not durable across a
worker restart. The worker serializes its own writes. A simultaneous standalone
writer retains the original UI's revision-check/save race; no stronger cross-process
atomicity is claimed.

Export-and-attach calls the public reviewed exporter, or verifies/reuses its existing
immutable output. Its fixed producer destination stays unchanged; our packager copies
the selected dependency/hash closure into the configured app exports root, preserving
artifact bytes and transformation history. Unrelated run histories are excluded.
Dependency indexing still reads hashes across the case's JSON files. Existing import
validation/supersession then runs unchanged. Old history remains; track mappings need
re-confirmation. If attachment fails after export, the UI identifies the completed
bundle and directs the reviewer to the existing attach flow.

## Verification and handoff

From the checkout root in the collaborator environment:

```sh
PYTHONPATH=.:src "$RECONSTRUCTION_PYTHON" -m pytest -q integration/tools_worker
PYTHONPATH=.:src "$RECONSTRUCTION_PYTHON" -m pytest -q tests
```

Worker parity tests need `INCIDENTDESK_TEST_CASES_ROOT` pointing at the original
MIG-02 synthetic cases, only to copy their videos/derived media into disposable
test cases. Live source cases are never edited by the tests. From the app folder,
run `bash scripts/check_all.sh` with its default approved tag; no guards are changed.

Browser smoke and independent critical acceptance belong to Claude. Follow
`integration/smoke/UI-STAGE-2-NATIVE-TOOLS.md`. Raw commands, results, negative
environment attempts and handoffs remain in the donor ticket result. The native
implementation does not imply browser acceptance or a passing original producer suite.

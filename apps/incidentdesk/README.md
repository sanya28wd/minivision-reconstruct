# Incident Video Analysis Report

This repo builds a tool that reads traffic-accident video (dashcam, CCTV, in-cabin; one or several cameras per incident) and writes an incident report for RTA Dubai. It is a BITS Pilani Dubai project supervised by Dr Shivang Agarwal.

Accident **reconstruction** (physics, trajectories) is built separately by another student. It will be combined with this module later. The reconstruction research kept here is for reference only.

## Status
The repo was cleared on 2026-10-11 so the build could start fresh; all earlier code is on git tag `pre-flush-2026-10-11`. Spec 001 is approved and its tickets (RPT-01 to RPT-54) are in `.agent_bridge/tasks/`. The first ticket, **RPT-01, the walking skeleton**, is the only application code so far: upload one video, get a minimal Report page, with the run logged. It uses a **recorded (replay) AI answer only**. No live model is called, and no key or credential is needed.

## Where things are
| Path | What it holds |
|---|---|
| `.claude/traffic-incident-agent.prd.md` | The PRD (start here). `docs/PRD.md` points to it. |
| `docs/context/` | Professor requirements and earlier project context |
| `dataset/videos/` | Benchmark clips. The video files are not in git; a backup is in `../incident-agent-archive/`. |
| `dataset/manifest.json` | The dataset manifest (RPT-05): each clip's source, licence, slices, synthetic-pair flag, truth status and fingerprint. See [`docs/DATASET_MANIFEST.md`](docs/DATASET_MANIFEST.md). |
| `dataset/truth/` | Protected ground truth, plus the Gemini dense-frame truth prompt |
| `dataset/reference/` | Sample RTA report, historical traffic data |
| `docs/reference/` | Reconstruction physics, UAE fault rules, prior specs |
| `docs/MASTER_DESIGN_DECISIONS.md` | Dated architecture decisions |
| `CLAUDE.md`, `AGENTS.md`, `docs/AGENT_COLLABORATION.md`, `docs/MODEL_ROUTING.md` | How Claude Code and Codex work together on this repo |
| `.agents/skills/` | Development skills (Matt Pocock suite + Addy Osmani). `.claude/skills` links here. |

## Run it

You need [uv](https://docs.astral.sh/uv/) (it installs the right Python and the locked packages) and a PostgreSQL server (16 was used for development). The skeleton needs a database for the run log; there is no SQLite fallback (ADR 0011).

**1. Install the packages** (from the lockfile, exactly):

```
uv sync --locked
```

**2. Make a local database and tell the app where it is.** `DATABASE_URL` is read from the environment. No password is stored in the repo.

Linux/macOS (bash), assuming your OS user is a PostgreSQL role that can create databases (`sudo -u postgres createuser --createdb "$USER"` once):

```bash
createdb incident_dev
export DATABASE_URL=postgresql:///incident_dev
```

Windows PowerShell, assuming PostgreSQL's `bin` folder is on PATH and the `postgres` user's password is in `PGPASSWORD` (libpq reads it, so it never appears in the URL):

```powershell
$env:PGPASSWORD = "<your postgres password>"
createdb -U postgres incident_dev
$env:DATABASE_URL = "postgresql://postgres@localhost:5432/incident_dev"
```

**3. Create the tables and start the app:**

```
uv run python -m incident_report.db migrate
uv run uvicorn --factory incident_report.web:create_app --port 8000
```

**4. Try it.** Make the sample upload and send it through the page at <http://localhost:8000/> (choose **Exterior**):

```
uv run python -m incident_report.samples sample.mp4
```

The page shows the Report: a summary line that comes from the shipped recorded response, which is labelled **synthetic** (hand-written, describing no real footage). Any other file has no recording, so the page says so plainly and nothing is invented; that failed attempt is logged as a Run too. The sample is a small placeholder file, not a real video: this ticket only fingerprints what it is given and never decodes video. (`sample.mp4` is gitignored; never commit media.)

To look at the run log: `psql incident_dev -c "select run_id, origin, status, total_latency_ms from runs"`.

## Test it

The tests use real PostgreSQL: each test session creates a throwaway database on the server named by `TEST_DATABASE_URL` (a role that may `CREATE DATABASE`; default `postgresql:///postgres`) and drops it afterwards. If PostgreSQL cannot be reached the tests **fail** with a plain message; they are never skipped. No live AI call is made.

```bash
uv run pytest
```

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres@localhost:5432/postgres"   # with PGPASSWORD set as above
uv run pytest
```

### Replay test harness (RPT-16)

The deterministic test suite in `tests/test_replay_harness.py` runs incident clips through Seam 1 (`analyse_incident`) using recorded AI responses and asserts strictly on the resulting `Report` data object. It requires no API quota and strictly blocks external network calls. See [`docs/REPLAY_HARNESS.md`](docs/REPLAY_HARNESS.md) for how to record new clips, file formats, and expected assertions.

## Migrations, backups and local evidence (RPT-03)

Full background, environments and the Cloud SQL cost check are in [`docs/STORAGE_AND_BACKUPS.md`](docs/STORAGE_AND_BACKUPS.md).

**Environment.** `INCIDENT_ENV` is `dev` (the default), `staging` or `production`. Each has its own database, bucket and credentials; agents only ever hold dev ones, and the owner promotes to production by hand.

**Migrations** (every migration has an up file and a `.down.sql` file). Rolling back deletes data, so it needs `--yes`; back up first.

```
uv run python -m incident_report.db status
uv run python -m incident_report.db migrate
uv run python -m incident_report.db rollback --steps 1 --yes
uv run python -m incident_report.db rollback --to 0001 --yes
uv run python -m incident_report.db rollback --to base --yes
```

(`--to 0001` keeps migration 0001 applied and undoes everything after it; `--to base` undoes everything. The same commands work in PowerShell.)

**Backup and verified restore.** You need the PostgreSQL client tools (`pg_dump`, `pg_restore`), the same major version as the server or newer. Keep backups outside the repository.

```bash
export INCIDENT_BACKUP_DIR="$HOME/incident-backups"
export SCRATCH_ADMIN_DATABASE_URL=postgresql:///postgres     # a server where a scratch database may be created
# export INCIDENT_PG_BIN_DIR=/usr/lib/postgresql/16/bin       # only if pg_dump is not on PATH
uv run python -m incident_report.backup backup                 # DATABASE_URL is the source
uv run python -m incident_report.backup restore-verify "$INCIDENT_BACKUP_DIR/<file>.dump"
```

```powershell
$env:INCIDENT_BACKUP_DIR = "$HOME\incident-backups"
$env:SCRATCH_ADMIN_DATABASE_URL = "postgresql://postgres@localhost:5432/postgres"   # PGPASSWORD set as above
# $env:INCIDENT_PG_BIN_DIR = "C:\Program Files\PostgreSQL\16\bin"                # if pg_dump is not on PATH
uv run python -m incident_report.backup backup
uv run python -m incident_report.backup restore-verify "$env:INCIDENT_BACKUP_DIR\<file>.dump"
```

`backup` writes a `.dump` and a `.dump.manifest.json` (per-table row counts and content checksums). `restore-verify` restores into a brand-new scratch database, compares, prints PASS or FAIL, and drops the scratch database (add `--keep` to look at it). It exits non-zero on any difference. It never restores into an existing database.

**Where local evidence goes.** Set `INCIDENT_EVIDENCE_DIR` to an absolute folder outside the source tree. Evidence is stored there by its SHA-256 and never changed. This local backend is for development and tests only: on Cloud Run the container filesystem is in-memory and not persistent, so the app refuses it in staging and production.

```bash
export INCIDENT_EVIDENCE_DIR="$HOME/incident-evidence"
```

```powershell
$env:INCIDENT_EVIDENCE_DIR = "$HOME\incident-evidence"
```

**Evidence intake limits (RPT-06, RPT-20).** Uploads are accepted only as MP4/MOV, WebM/Matroska or AVI, confirmed by the file's first bytes and by `ffprobe` (the extension and the browser's Content-Type are never trusted). Every cap and timeout is an environment setting, and every default is a project assumption, to be validated (ADR 0006): `INCIDENT_MAX_UPLOAD_BYTES` (536870912), `INCIDENT_MAX_TOTAL_UPLOAD_BYTES` (1073741824), `INCIDENT_MAX_FILES` (10), `INCIDENT_MAX_FIELDS` (20), `INCIDENT_MAX_DURATION_SECONDS` (3600), `INCIDENT_UPLOAD_TIMEOUT_SECONDS`, `INCIDENT_HASH_TIMEOUT_SECONDS`, `INCIDENT_STORE_TIMEOUT_SECONDS`, `INCIDENT_PROBE_TIMEOUT_SECONDS` (30), `INCIDENT_COPY_TIMEOUT_SECONDS` (each 120 unless noted), plus `INCIDENT_MAX_PROBE_BYTES`, `INCIDENT_MAX_FORM_BYTES` and `INCIDENT_STAGE_WORKERS`. `ffprobe` (part of ffmpeg) must be on PATH.

Not built yet (owner steps): the Cloud Storage backend and bucket, Cloud SQL with automated backups and point-in-time recovery, and the restore rehearsal on Cloud SQL.

## Dataset manifest (RPT-05)

`dataset/manifest.json` records, for every clip, its source, licence, slices (night, rain, CCTV vs dashcam, number of views, interior present), whether it is a synthetic interior+exterior pair, how far its truth is reviewed, and a SHA-256 fingerprint of the video. Archived clips (the old ones, kept for spot-checks) are in the manifest but are **never** part of the benchmark unless you ask for them explicitly. The schema, every rule, and the open questions are in [`docs/DATASET_MANIFEST.md`](docs/DATASET_MANIFEST.md). No video is ever committed.

Check it (exits non-zero and lists every problem if anything is wrong), and list a selection:

```bash
uv run python -m incident_report.manifest validate
uv run python -m incident_report.manifest list                       # the benchmark (archived clips excluded)
uv run python -m incident_report.manifest list --set spot-check      # archived clips, asked for explicitly
```

```powershell
uv run python -m incident_report.manifest validate
uv run python -m incident_report.manifest list
uv run python -m incident_report.manifest list --set spot-check
```

**Owner step (needs the real video files): fill in the fingerprints.** Run from the repository root on the machine that has the videos. It fills empty fingerprints and refuses to overwrite a recorded one that no longer matches the file (it prints `MISMATCH` and writes nothing).

```bash
uv run python -m incident_report.manifest fingerprint --check     # look first, write nothing
uv run python -m incident_report.manifest fingerprint             # fill the empty ones
git diff dataset/manifest.json
```

```powershell
uv run python -m incident_report.manifest fingerprint --check
uv run python -m incident_report.manifest fingerprint
git diff dataset/manifest.json
```

**Adding a benchmark clip (RPT-10):** put the file in `dataset/videos/`, add an entry to the manifest with its real source and licence (copy the template in the docs), run `validate`, then `fingerprint`. A benchmark entry may not say `unknown` for source, licence or any slice; only the archived spot-check entries may. The source and licence of the archived clips are not recorded anywhere in the repository: they say `unknown — owner to confirm` until you fill them in.

## CI and guardrails (RPT-02)

Every push and pull request runs format, lint, strict type checks, the tests (with a PostgreSQL service container), a dependency audit, a secret scan, a protected-path check and a test-integrity check. Each is one command you can run yourself:

```
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy
uv run pytest
uv run python scripts/guard_protected_paths.py --base origin/main
uv run python scripts/guard_test_integrity.py --base origin/main
uv run python scripts/guard_secrets.py --base origin/main
uv run python scripts/check_dependencies.py --base origin/main
```

To block a secret before it is even committed, install the hook once with `uv run pre-commit install`. The full list (including `pip-audit`), the approval labels and the owner's setup steps are in [`docs/CI_GUARDRAILS.md`](docs/CI_GUARDRAILS.md). The same commands work in Windows PowerShell.

## How the code is laid out

| Path | What it holds |
|---|---|
| `src/incident_report/entry.py` | **Seam 1**, the incident entry point `analyse_incident(views, origin, ai=, run_log=)`. Returns the Report and writes one Run. The web route and the future benchmark harness call only this. |
| `src/incident_report/ai/` | **Seam 2**, the AI connection (`interface.py`) and its only adapter so far, the recorded/replay one (`replay.py`). Recordings live in `ai/recordings/`, keyed by input hash + prompt version; each hand-made one says `"provenance": "synthetic"`. |
| `src/incident_report/domain.py` | `View`, `Origin` (app / benchmark / replay), `Role` (interior / exterior), `Report`, `Run`. |
| `src/incident_report/runlog.py`, `db.py`, `migrations/` | The run log in PostgreSQL and the reversible migration runner (every migration has an up and a down file). |
| `src/incident_report/storage/` | The evidence storage seam (`EvidenceStore`: write-once, SHA-256 keyed) and its local development backend. A Cloud Storage backend is still to come (owner step). |
| `src/incident_report/manifest/` | The dataset manifest (RPT-05): types and clip selection (`model.py`), the validator (`validation.py`), the owner's fingerprint command (`fingerprint.py`), and `python -m incident_report.manifest` (`__main__.py`). |
| `src/incident_report/backup.py` | Logical backup (`pg_dump`) and verified restore into a scratch database. |
| `src/incident_report/web/` | FastAPI routes and Jinja2 templates. A page is rendered from the Report object alone, with escaping always on. Every template extends the shared shell `templates/base.html`; styles, tokens and self-hosted fonts are in `static/`, the bilingual label catalogue is `labels.json` (RPT-04). |
| `tests/` | pytest tests through the two seams and the HTTP interface, and `test_guard_*.py` for the CI guards. |
| `scripts/` | The CI guard scripts (protected paths, test integrity, secrets, dependencies, owner approval). Each takes a base and a head, so it runs the same locally and in CI. |
| `.github/workflows/ci.yml` | The GitHub Actions workflow. |
| `docs/UI_FOUNDATION.md`, `THIRD_PARTY_NOTICES.md` | The UI shell, design tokens and where each came from, the font decision, bilingual labels, right-to-left and accessibility rules; the MIT (UAE Design System) and OFL (fonts) licences (RPT-04). |
| `docs/CI_GUARDRAILS.md` | What CI checks, how to run it all yourself, the owner steps (branch protection, approval labels), and the limits. |

Conventions every later ticket follows: tests go through a seam (the entry point, the AI connection, HTTP), not internals; new code goes under `src/incident_report/`; configuration comes from environment variables (`DATABASE_URL`, optional `INCIDENT_RECORDINGS_DIR`, `INCIDENT_ENV`, `INCIDENT_EVIDENCE_DIR`); a missing recording or other failure is a plain error, never a made-up answer.

## Tooling choices (RPT-01, RPT-02)

- **Python 3.13 by default, 3.12 or newer supported** (`requires-python = ">=3.12"`, `.python-version` is 3.13). The Gen AI SDK comes later and targets current Python 3; 3.13 is what the development machine has. Revisit when the container image is chosen.
- **uv with `uv.lock`.** One lockfile, with a SHA-256 for every package, that `uv sync --locked` installs exactly. For the dependency audit (RPT-02), `uv export --frozen --no-emit-project --format requirements-txt` writes a hashed `requirements.txt` that `pip-audit` can read. The project metadata is plain `pyproject.toml`, so nothing here locks you to uv.
- **FastAPI + Jinja2 + python-multipart.** FastAPI gives typed form and upload handling and a test client; Jinja2 matches the spec's server-rendered templates (HTMX is added with the UI ticket). Jinja2 autoescape is switched on explicitly.
- **psycopg 3 (`psycopg[binary]`) and plain SQL.** PostgreSQL only. The `binary` build ships its own libpq, so no compiler is needed. I could not reach the psycopg install docs from the build sandbox to confirm whether they advise it for production; the deployment ticket should check that before choosing the container image.
- **pytest and httpx** (dev only), with a real database rather than mocks.
- **No new Python packages in RPT-03.** Migrations stay plain SQL on the existing runner; backups call the PostgreSQL client tools (`pg_dump`, `pg_restore`), which are system programs, not Python dependencies. CI installs `postgresql-client-16` for them.
- **ruff, mypy, pip-audit, detect-secrets, pre-commit** (dev only, RPT-02): format and lint, strict types, known-vulnerability audit, secret patterns, and the commit hook. `pydantic` is now declared directly because the code imports it (RPT-01 review, finding 3).

## Known limits of the skeleton (by design)

- Uploads are fingerprinted (SHA-256) as they stream in and are never written by this code. The web framework spools each upload to a temporary file (memory up to 1 MB, then the OS temp directory) that is deleted when the request ends; storing uploads as evidence is RPT-06 (the storage seam itself is RPT-03). There is no upload size or duration cap, no video validation and no access control yet (share links are RPT-09).
- One page, no UI shell or banners (RPT-04), no live model (RPT-07).
- A report is looked up by its random run id only to make the page reloadable; real access rules come with RPT-09.


## Public-research Gemini overview (RPT-07)

The web app remains replay-only. The explicit owner smoke command exercises the one-view live seam and records raw responses, request hashes, prompt version, latency and retry counts in PostgreSQL. The Report carries a minimal cited-facts stub, without fault or fines. Replay uses ReplayOverviewAIConnection(settings, saved_run.calls).

The official `google-genai` SDK (2.31.0, locked in `uv.lock`) is the only Gemini dependency. Its calls were checked against the installed package source; see [the source note](docs/research/RPT07_GEMINI_SDK.md).

Small videos are sent inline by default. The transport counts base64 padding for every video part,
text/system instructions, schema/configuration and a 4 KiB envelope margin, and uses the Files API
when that count reaches 20,000,000 bytes. Set `GEMINI_VIDEO_INLINE=0` to force the Files API for
comparison (also accepts `false`, `no`, `off`); unset it to restore inline selection. This setting
does not affect request hashes, prompts, sampling or backend selection. Inline calls have no
`upload` or `processing_wait` timing stages. Inline provider errors retain status fields only,
because provider messages could echo video bytes. See [official limit sources](docs/research/PERF04A_INLINE_LIMITS.md).

The owner-pinned value is configured explicitly, without a model default. Floating aliases ending in -latest or lacking a numeric version are refused. The key must already be in GEMINI_API_KEY or GOOGLE_API_KEY; this command never reads .env. Use a separate, migrated dev database for smoke Runs (for example, `incident_smoke`), never the app or test database. With the owner present, on a manifest-registered public research MP4 or an explicitly declared synthetic clip:

Key names: Set GEMINI_API_KEY from one of your keys, e.g. `$env:GEMINI_API_KEY = $env:GOOGLE_API_KEY_1` (this copies the value without printing it). The command reads only GEMINI_API_KEY or GOOGLE_API_KEY, so a key stored under another name, such as GOOGLE_API_KEY_1 to GOOGLE_API_KEY_6, must be copied first.

~~~powershell
$env:GEMINI_API_KEY = $env:GOOGLE_API_KEY_1  # or paste a key for this session only; never save it in a file
$env:PGPASSWORD = "<local dev database password>"  # libpq reads this; no password in the URL
$pgBin = "C:/Users/HP/tools/pgsql16/pgsql/bin"  # adjust to your PostgreSQL tools
# First setup only: omit createdb if incident_smoke already exists.
& "$pgBin/createdb.exe" -h localhost -p 5433 -U postgres incident_smoke
$env:DATABASE_URL = "postgresql://postgres@localhost:5433/incident_smoke"
$env:INCIDENT_ENV = "dev"
$smokeScratch = Join-Path $env:TEMP "incident-smoke"
$env:INCIDENT_EVIDENCE_DIR = Join-Path $smokeScratch "evidence"
New-Item -ItemType Directory -Force -Path $env:INCIDENT_EVIDENCE_DIR | Out-Null
$env:GEMINI_MODEL = "gemini-3.8-flash"
$env:GEMINI_OVERVIEW_FPS = "1"
$env:GEMINI_MAX_RETRIES = "0"  # one model attempt for the owner smoke check
uv run python -m incident_report.db migrate
uv run python -m incident_report.ai.smoke "C:/public-research/registered-clip.mp4" --role exterior --public-research
~~~

To make two moving synthetic clips locally, run these exact PowerShell commands after the setup above (`ffmpeg` must be on PATH). `overlay` evaluates positions per frame; `drawbox` position expressions are evaluated once and do not animate the boxes. The red and blue boxes approach and overlap in the first clip; the white and green boxes pass in separate rows in the second. These are abstract motion checks, with no vehicle or fault ground truth. The commands refuse to overwrite existing files (`-n`); use a new scratch folder for another set.

~~~powershell
ffmpeg -n -v error -filter_complex_threads 1 -f lavfi -i "color=c=black:s=320x180:r=10:d=6" -f lavfi -i "color=c=red:s=24x16:r=10:d=6" -f lavfi -i "color=c=blue:s=24x16:r=10:d=6" -filter_complex "[0:v][1:v]overlay=x='20+36*min(t,3)':y=82:eval=frame:shortest=1[red];[red][2:v]overlay=x='244-36*min(t,3)':y=82:eval=frame:shortest=1[out]" -map "[out]" -an -c:v libx264 -pix_fmt yuv420p (Join-Path $smokeScratch "synthetic-contact.mp4")
ffmpeg -n -v error -filter_complex_threads 1 -f lavfi -i "color=c=black:s=320x180:r=10:d=6" -f lavfi -i "color=c=white:s=24x16:r=10:d=6" -f lavfi -i "color=c=green:s=24x16:r=10:d=6" -filter_complex "[0:v][1:v]overlay=x='20+36*t':y=40:eval=frame:shortest=1[white];[white][2:v]overlay=x='244-36*t':y=124:eval=frame:shortest=1[out]" -map "[out]" -an -c:v libx264 -pix_fmt yuv420p (Join-Path $smokeScratch "synthetic-no-contact.mp4")
# Owner-present live call only: each invocation sends a video and uses model quota.
uv run python -m incident_report.ai.smoke (Join-Path $smokeScratch "synthetic-contact.mp4") --role exterior --public-research
uv run python -m incident_report.ai.smoke (Join-Path $smokeScratch "synthetic-no-contact.mp4") --role exterior --public-research
~~~

For a 504 deadline failure, increase `GEMINI_HTTP_TIMEOUT_MS` explicitly before another owner-present attempt; the default remains 60000 ms. [Google's troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting) links to an [error reference scoped to the Interactions API](https://ai.google.dev/gemini-api/docs/api-errors), whose 504 guidance recommends removing or increasing the client deadline. The overview keeps its existing retry policy for 429 and 503.

A failed model call keeps a sanitized `provider_error` on its stored call record (HTTP status, provider status such as UNAVAILABLE, and the provider message cut to 300 characters; keys, headers, URLs and the uploaded file address are removed). The message shown to people stays plain. Replace the example file path with the registered public clip. No live calls were made during implementation. Omission of --public-research refuses before SDK creation. Real/NDA footage stays blocked pending RPT-12 and owner-confirmed Vertex zero retention. Sampling/retry/timeout defaults are configurable and labelled **project assumption, to be validated**; see [sources and settings](docs/research/RPT07_GEMINI_SDK.md). Full citation-range validation is RPT-11; the stub uses SHA-256 as view ID.

Migration 0003 adds Run call records and retry counts. Its down migration discards those fields; back up before rollback. Migration 0002 may be absent here. After merge, apply all migrations normally.


### Early upload staging (PERF-08)

With JavaScript, picking a file starts an authenticated temporary upload while the
officer fills in the form. The original file selection remains available for normal
submission if staging fails or its ID expires. No evidence or intake audit record is
published until submit; submit follows the same hashing, probing and evidence path.
`INCIDENT_STAGING_TTL_SECONDS` defaults to 900 seconds (a project assumption under
ADR 0006) and must be positive and strictly below the provider's 48-hour retention.
Stages also have per-session count/byte caps and a process-wide count cap based on
`IntakeLimits`. Expiry and graceful shutdown close local temporary handles and delete
provider files. Failed remote deletion warns; Google's retention is the backstop.

The default replay app stages locally. Provider preparation is enabled only for an
existing `GeminiAIConnection` with explicitly declared public footage and the same
SHA-256, using a client explicitly configured for the Developer API. Arbitrary/NDA
uploads never gain a public declaration through this endpoint; Vertex Files are not
supported. An ACTIVE preparation forces Files despite the inline default. Pending or
failed preparation falls back to normal transport. Stage IDs are process-local: a
restart or another worker causes a safe retry using the retained browser bytes. A
process crash may leave a remote file until Google's 48-hour deletion backstop; this
is an in-process optimization, not a durable upload service. See
[provider sources](docs/research/PERF08_EARLY_UPLOAD.md).

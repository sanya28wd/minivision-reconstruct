# Starting IncidentDesk

Two components use separate locked environments and exchange files only.

## Report app â€” Windows PowerShell

Create a dedicated PostgreSQL database `incidentdesk_dev` and a separate `incidentdesk_test_admin` database with a role allowed to create throwaway test databases. Use local credentials through libpq; never embed passwords in URLs. Set `DATABASE_URL` to the dedicated development database. Set `TEST_DATABASE_URL` separately for tests (see app `.env.test.example` comments). Never use the donor database.

```powershell
cd D:/minivision-incidentdesk/minivision-reconstruct/apps/incidentdesk
$env:DATABASE_URL = 'postgresql://postgres@127.0.0.1:5433/incidentdesk_dev'
uv sync --locked
uv run python -m incident_report.db migrate
uv run uvicorn --factory incident_report.web:create_app --port 8000
# Gate (Git Bash): bash scripts/check_all.sh
# PowerShell gate: ./scripts/check_all.ps1 -Base integration/incidentdesk
```

Replay samples at http://127.0.0.1:8000/samples require no model API key. Authenticate using the app's existing local admin/session flow. Do not enable live model processing for migration checks.

## Reconstruction worker â€” WSL Ubuntu

The existing user-space tools live outside the repository. No worker source changes are required. If a tool or locked wheel is missing, provision it separately; do not resolve a new lock or download models as part of checks.

```bash
cd /mnt/d/minivision-incidentdesk/minivision-reconstruct
export PATH=/mnt/d/minivision-incidentdesk/linux-tools/uv:/mnt/d/minivision-incidentdesk/linux-tools/ffmpeg/bin:$PATH
export UV_PROJECT_ENVIRONMENT=/mnt/d/minivision-incidentdesk/.venv-linux
export UV_CACHE_DIR=/mnt/d/minivision-incidentdesk/.uv-cache-linux
export PYTHONPATH=src
uv sync --locked --extra dev
uv run --locked python scripts/process_case.py --input <mp4> --case-id <id> --cases-root /mnt/d/minivision-incidentdesk/cases --ffprobe ffprobe
uv run --locked streamlit run scripts/review_tracks.py -- --cases-root /mnt/d/minivision-incidentdesk/cases
uv run --locked streamlit run scripts/calibrate_case.py -- --cases-root /mnt/d/minivision-incidentdesk/cases
uv run --locked streamlit run scripts/reconstruct_case.py -- --cases-root /mnt/d/minivision-incidentdesk/cases
# Remaining pipeline/export commands and flags: fork README.md.
uv run --locked pytest -q -rfE -p no:cacheprovider
```

## Shared cases

Windows `D:/minivision-incidentdesk/cases` maps to WSL `/mnt/d/minivision-incidentdesk/cases`. Reports must never store either absolute path. Relative evidence identifiers and the importer boundary are MIG-02 work, not implemented here.

## Review gate

The offline dependency gate compares committed app lock blobs at the selected base and head. An unchanged committed lock needs no index audit. A changed committed lock fails closed offline; use the normal online dependency audit only in a separately authorized dependency review. Working-copy content does not decide this gate. No automatic blanket protected-path approval is applied.

## Reviewed gate baseline

Create ticket branches from `integration/incidentdesk`. The default gate base is the owner-created annotated tag `incidentdesk-import-approved`. Only the owner creates or moves that tag after review. Missing, lightweight, non-ancestor tags and empty ranges are refused. Until approval, use `bash scripts/check_all.sh --Base integration/incidentdesk` or PowerShell `./scripts/check_all.ps1 -Base integration/incidentdesk`; `BASE` also overrides the default. Checks report all thirteen results, including expected protected guard edits requiring review.

The separate `collaborator paths unchanged` step compares the selected base through head for the worker source, root scripts/configs/tests/evaluation/data, and root project metadata. It reports changed paths and fails. Root scripts remain watched by the protected-path guard, preserving the imported guard test contract.

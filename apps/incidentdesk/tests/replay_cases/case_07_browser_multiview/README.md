# Synthetic browser journey

Two valid synthetic MP4 views with inert RPT-42 markers and hand-written, per-view
overview-2 recordings. These are workflow fixtures, never AI output, real footage,
or an accuracy benchmark. Each cited party is at timestamp 0 in its own view.
The deterministic rule engine produces Cannot Determine from insufficient facts;
the browser officer then overrides one finding, supplies a reason and approves.

Run from the repository root with PostgreSQL configured:

```powershell
uv sync --locked
uv run playwright install chromium
uv run pytest tests/test_browser_e2e.py tests/test_flagged_owner_ruling.py -q
```

The same browser journey is explicitly opt-in for public research footage:

```powershell
# Supply a pinned GEMINI_MODEL and GOOGLE_API_KEY_2 through 6 in the process environment.
# GEMINI_API_KEY_PAID is preferred if available. Only HTTP 429 rotates to the next key.
# Pytest does not open .env. Never print the key or put it in a command argument.
$env:INCIDENT_E2E_LIVE = '1'
$env:INCIDENT_E2E_ARCHIVE = 'dataset/archive/videos/MINOR/MINOR_01.mp4'
uv run pytest tests/test_browser_e2e.py -q --tb=short
Remove-Item Env:INCIDENT_E2E_LIVE
```

Live mode uploads exterior only. Both modes use the same app, database, evidence
store, share-link exchange, browser actions and assertions. Missing Chromium fails
with its installation command; nothing is skipped. On a browser-stage failure,
`results/e2e/<mode>-failure.png` and `<mode>-server.log` are saved. Setup failures
that occur before a browser exists cannot produce a browser screenshot. CI
installs Chromium and uploads failure artifacts; `scripts/check_all.sh` is unchanged.
The app is explicitly constructed with `prompt_version="overview-2"` for both
modes. Its existing default remains `skeleton-1` for legacy deployments.
The output PDF and mode outcome/timing files are also saved locally under the
gitignored `results/e2e/` folder.

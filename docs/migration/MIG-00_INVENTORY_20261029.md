# MIG-00 inventory and frozen starting point

Date: 2026-10-29. Card: MIG-00 in `docs/MINIVISION_INCIDENTDESK_CLAUDE_MIGRATION_HANDOFF_20261029.md` (section 8).
Author: Claude Code (Opus 5.5). Reviewer: not yet reviewed (collaborator reviews the reconstruction rows).

No code was migrated, no dependency was upgraded, no model was called, nothing was pushed. Both repositories' tracked files are unchanged. The only new things are this document, a durable fork clone on D:, and locked test environments (listed in section 1).

## 1 Frozen starting point

| Item | Donor: incident agent | Fork: minivision-reconstruct |
|---|---|---|
| Local checkout | `C:/Users/HP/Desktop/UNI/projects/SHIVANG PROJECT/incident-agent` | **Durable (new):** `D:/minivision-incidentdesk/minivision-reconstruct` (full clone, not shallow). Old shallow clone `C:/Users/HP/AppData/Local/Temp/minivision-inspect-20261029` is disposable. |
| Branch | `fresh-start` | `main` |
| HEAD | `347a778576861ef972236781bc3bd5a2f4714ba3` | `c66000af37099e39920ff0e8ac621ce5be09090d` |
| Remotes | `origin` = github.com/josephjose-dev/RTA-ACCIDENT-AGENT | `origin` = github.com/josephjose-dev/minivision-reconstruct; `upstream` = github.com/sanya28wd/minivision-reconstruct |
| Remote heads (fetched 2026-10-29) | not re-fetched | `origin/main` = `upstream/main` = `c66000a`; 0 commits ahead/behind. Single commit history ("Initial commit"). Matches the inspection-note snapshot. |
| Dirty files | 40 untracked, 0 modified tracked: `.Codex/sessions/*` and `.claude/sessions/*` handoffs (13 each), 4 `.agent_bridge/lanes/*.status`, 2 results, 2 reviews, 1 task card (`REA-VIDEO-LOCAL-20261014`), the three migration docs, `output/`, `results/report-gallery-2026-10-28/`. All preserved untouched. | Clean (0 changes; `.venv/` is gitignored). |
| Instructions in force | `CLAUDE.md` (Codex: yes; ADR 0004/0006/0009/0011 guardrails; protected paths), `AGENTS.md`, `CODING_STANDARDS.md`, `docs/agents/SKILL_ROUTING.md` | No `CLAUDE.md`/`AGENTS.md`. Rules live in `README.md` and `IMPLEMENTATION_PLAN.md` (locked test split, no speed from video, no legal fault). No CI (`.github/` absent). |
| Python | `.python-version` 3.13; `requires-python >=3.12` | `requires-python >=3.11`; author runs a macOS `.week6-venv` (README line 230) |
| Environments made for MIG-00 | existing `.venv` (unchanged) | Windows: `.venv` in the checkout, `uv sync --locked --extra dev --python 3.12`, cache `D:/minivision-incidentdesk/.uv-cache`. Linux (WSL Ubuntu): `D:/minivision-incidentdesk/.venv-linux`, user-space `uv` + static ffmpeg under `D:/minivision-incidentdesk/linux-tools`. Everything on D: because C: is 97 % full (18 GB free). |

`--locked` succeeded on both platforms' installs, so the checked-in `uv.lock` matches `pyproject.toml`; nothing was re-resolved.

## 2 Baseline checks (exact commands and results)

### Donor (Windows 11, native)

```powershell
# from the incident-agent checkout
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_all.ps1
```

BASELINE_AGENT

### Fork (Windows 11, native)

```bash
cd D:/minivision-incidentdesk/minivision-reconstruct
export UV_CACHE_DIR=D:/minivision-incidentdesk/.uv-cache
uv sync --locked --extra dev --python 3.12
uv run --locked pytest -q -rfE -p no:cacheprovider
```

Result: **9 of 11 test modules fail at import** with `ModuleNotFoundError: No module named 'fcntl'`. 0 tests ran from them. The two modules that import cleanly pass:

```text
uv run --locked pytest -q -rfE -p no:cacheprovider tests/smoke/test_evidence_ingestion.py tests/smoke/test_tracking_states.py
6 passed in 8.65s
```

So native Windows runs 6 of 69 tests (`grep -c "def test_"` over `tests/smoke/`). Windows ffmpeg/ffprobe 8.1 were on PATH.

### Fork (Linux, WSL Ubuntu 24.04, Python 3.12)

Script: `C:/Users/HP/AppData/Local/Temp/claude/c--Users-HP-Desktop-UNI-projects-SHIVANG-PROJECT-incident-agent/d262c1bd-bf0d-4d16-9c49-1b097060eea2/scratchpad/linux_baseline.sh` (same checkout via `/mnt/d`, separate venv, `uv sync --locked`, then the same pytest command).

BASELINE_LINUX

## 3 Platform findings (native Windows vs Linux)

| Finding | Evidence | Effect |
|---|---|---|
| `fcntl` used in **three** modules, not two | `incident/review.py:11` (`flock` at 114), `perception/review_workflow.py:7` (154, 179), **`reconstruction/storage.py:4`** (missed by the inspection note) | Everything that imports review, reconstruction pipeline (`reconstruction/pipeline.py:23` imports `review_workflow`), demo, Streamlit review UIs, incident UI is unusable on native Windows. Confirmed by execution above. |
| ffmpeg + ffprobe are runtime requirements of both projects | Fork: `evidence/metadata.py` subprocess, fixtures in `*/staged_fixture.py`; donor: `ai/motion.py`, `ai/crash_finder_motion.py` | Must be on PATH (or passed as `--ffprobe`) in every environment, including WSL. WSL Ubuntu had none; no passwordless sudo, so a user-space static build was used. |
| Playwright browsers | Fork reconstruction export renders via Playwright (`PLAYWRIGHT_BROWSERS_PATH` in README lines 171, 191); donor uses Playwright only in dev tests (`test_browser_e2e.py`) | Fork export needs a browser download per environment. Not installed in MIG-00 (not needed by the collected tests; see Linux result). |
| Canvas frontend is prebuilt | `reconstruction/component/frontend/dist/{renderer.js,workspace.js,workspace.css}` checked in; source in `src/*.ts`, Vite build, `package-lock.json` | No Node build needed to run. Rebuild needs Node (present on Windows via nvm4w; absent in WSL). |
| Donor needs PostgreSQL | `psycopg`, migrations `0001`–`0019`, `check_all` uses `TEST_DATABASE_URL` (local service `PostgreSQL_For_Odoo` running) | The reporting app needs a Postgres per environment; fork has no database (file-based `cases/`). |
| Torch | fork `uv.lock`: torch 2.14.0, torchvision 0.29.0, ultralytics 8.4.149 from PyPI | Windows gets the CPU wheel. Linux PyPI torch pulls CUDA runtime wheels (several GB). WSL's disk image lives on C:, so the Linux venv was placed on `/mnt/d`. Matches standing rule: no CUDA in project envs without owner choice. |
| Paths | Fork commands assume POSIX (`.week6-venv/bin/python`, `PYTHONPATH=src`); donor has `.ps1` + `.sh` twins | Shared docs must give both forms, or pick WSL as the only worker shell. |

## 4 Dependency findings

| Package | Fork | Donor | Conflict? |
|---|---|---|---|
| starlette | `>=0.49,<1` (locked 0.52.1) | `>=1.7.0,<1.8` | **Yes, no overlap.** Confirmed from both `pyproject.toml` files. |
| Python | `>=3.11` | `>=3.12` (pinned 3.13) | No, 3.12/3.13 satisfy both; does not fix starlette. |
| pydantic | `>=2.10,<3` | `>=2.14.1` | No |
| uvicorn | `>=0.34,<1` (locked 0.54.0) | `>=0.54.0` | No |
| playwright | runtime dep `>=1.51,<2` | dev only `>=1.64.0` | No |
| streamlit | `>=1.53,<2` (locked 1.65.0); this is why starlette `<1` is needed | none | — |
| fastapi | none | `>=0.143.0` (needs starlette 1.x) | — |

Conclusion: keep **two locked environments** (handoff section 3). Nothing should import across them; the boundary is files on disk (the evidence contract). The donor's web app does not need any fork package to read JSONL/JSON artifacts.

Licences (inventory only, not a review): fork has **no LICENSE file** and declares none in `pyproject.toml`; its `ultralytics` dependency is **AGPL-3.0** (applies if the worker is distributed or offered as a network service). Donor has `THIRD_PARTY_NOTICES.md` and no LICENSE file. Decision 7 must cover both.

## 5 Fork inventory (reconstruction side)

Package `src/minivision_reconstruct/`: `domain/`, `evidence/` (ingest + ffprobe), `video/`, `calibration.py`, `perception/` (detect, track, review workflow + Streamlit UI), `reconstruction/` (calibration models, storage, replay, export, Canvas component), `movement/`, `incident/` (conflicts, events, assessments, review, UI). 258 tracked files; `evaluation/` 55 MB (PNGs, CSV, JSON results); `data/` only `registry.csv` tracked; `cases/`, `models/`, `data/*` gitignored (no weights or videos in git).

Commands (all `scripts/`, from README):

| Stage | Command | Kind |
|---|---|---|
| Ingest | `python scripts/process_case.py --input <mp4> --case-id <id> --cases-root cases --ffprobe ffprobe` | CLI |
| Detect / track | `scripts/detect_case.py`, `scripts/track_case.py` (YOLOv8n + ByteTrack configs in `configs/`) | CLI |
| Track review | `streamlit run scripts/review_tracks.py -- --cases-root cases` | Streamlit, needs `fcntl` |
| Calibration | `streamlit run scripts/calibrate_case.py -- --cases-root cases` | Streamlit |
| Reconstruction | `scripts/prepare_reconstruction.py`; `streamlit run scripts/reconstruct_case.py`; `scripts/export_reconstruction.py` (Playwright) | CLI + Streamlit/Canvas |
| Movement | `scripts/analyze_movement.py` | CLI |
| Incident | `scripts/analyze_incident.py`; review via incident UI | CLI + Streamlit, needs `fcntl` |
| Evaluation | `scripts/evaluate_{tracking,movement,incident,accident,ua_detrac}.py`, labelling apps | judges only |

Artifact exports (the importer's inputs) are listed in `docs/research/MINIVISION_FORK_CODE_INSPECTION_20261029.md`, section "Implemented artifact contracts". Restated limit: the presentation export lacks movement/incident/review JSONL, and the reviewed export references upstream manifests it does not package.

**Implemented vs measured vs planned (fork, from its own README "Next steps" and plan):**

| Capability | Status |
|---|---|
| Ingest, detection, tracking, track review, calibration, bird's-eye replay + export, movement, incident candidates, contribution proposals, reviewed revisions | Implemented; tests exist (see baseline) |
| Accuracy on controlled scripted scenes | Measured (checked-in metrics; synthetic, not CCTV) |
| Accuracy on CARLA accident held-out (25 clips) | Measured: 0 detections within tolerance |
| Real-footage evaluation (100 clips, 40 dev / 60 locked test) | Selection locked 2026-10-28; labels and results **not done** |
| Image-space vehicle-size thresholds, fragment linking, occluded-impact fix | Planned (Phase A) |
| One clickable timeline, PDF report, OCR road context, privacy-redacted export, case package | **Planned by the fork itself (Phase B)** — overlaps the handoff's single report/timeline owner (Joseph). Must be agreed before either side builds it. |

## 6 Donor inventory (reporting side)

Start commands (README): `uv run python -m incident_report.db migrate`; `uv run uvicorn --factory incident_report.web:create_app --port 8000`; sample: `uv run python -m incident_report.samples sample.mp4`. Checks: `scripts/check_all.ps1` / `.sh` (ruff format, ruff, mypy strict, pytest, 8 guard scripts).

Resource-path assumptions that matter when moving to `apps/incidentdesk/`:

| Location | Assumption | Moves cleanly? |
|---|---|---|
| `web/app.py:91` static, `web/templating.py:16` templates, `web/labels.py:24`, `fines/__init__.py:19`, `ai/replay.py:23` recordings | `Path(__file__)` package-relative | Yes |
| `db.py:48` migrations, `ai/overview10.py:60` prompts | `importlib.resources` | Yes |
| `sample_reports.py:43` | `parents[2] / "samples"` = app root | Yes if `samples/` goes to `apps/incidentdesk/samples/` |
| `manifest/validation.py:34`, `truth_draft/*.py` | `parents[3]` = repo root, then `dataset/…` | Only if `dataset/` comes too (evaluation code) |
| `benchmark/*` | `REPO_ROOT / dataset/…`, `results/…` | Evaluation code; same |
| Guards `scripts/guard_protected_paths.py:42-45` | root-relative `dataset/truth/`, `docs/adr/`, `docs/reference/`, `.github/`, `scripts/` | **No**: need a scoped re-root to `apps/incidentdesk/` (reviewed change, not deletion) |

Runtime seam (checked by grep): `web/` and `entry.py` never import `benchmark`, `manifest` or `truth_draft`. Only the owner CLI `override_labels.py` imports `manifest`. So the app can move without the evaluation tooling.

Test dependencies (grep of `tests/*.py`, 150 entries): 25 test files read `dataset/` or `REPO_ROOT`, 7 read `results/`, 11 read `docs/`, 8 read `specs/`/`CLAUDE.md`/`GLOSSARY.md`. Moving only the app would leave these failing or require skipping, which the handoff and `CLAUDE.md` forbid without owner approval. See decision D8.

Report/export behaviour (what really exists):

| Capability | Status in donor |
|---|---|
| HTML report, review timeline, per-view player, citations | Implemented (`/reports/{id}`, `/reports/{id}/review`, `/reports/{id}/views/{view}`) |
| Print view | Implemented: `/reports/{id}/print` + `static/css/print.css` (browser "Print to PDF") |
| Server-generated PDF | **Not implemented** (no PDF library, no PDF route) |
| Evidence bundle / case package (zip + manifest) | **Not implemented** |
| Report versions + approval | Implemented (`/reports/{id}/versions`, `/approve`, `/overrides`; migration 0016) |
| Officer questions | Implemented (0010, 0018) |
| Access | Share links + admin sessions (0004). **Not** role-based. |
| Intake | Video only, up to 10 views per incident, 512 MB each (`intake_limits.py`). No photos, documents or statements. |
| Fixtures | `tests/replay_cases/` (7 cases, 131 KB), `tests/fixtures/overview10_notes`, `samples/` (recordings + manifest, 124 KB), `ai/recordings/` (1 skeleton). All recorded, no API key needed. |

## 7 Import manifest (proposal for MIG-01, nothing copied yet)

Source: `josephjose-dev/RTA-ACCIDENT-AGENT` at `347a778576861ef972236781bc3bd5a2f4714ba3`. Destination root: the fork's integration branch. Git object IDs below let a reviewer verify the copied tree is byte-identical (`git ls-tree 347a778 <path>`).

| Source path | Git object | Destination | Action | Reason |
|---|---|---|---|---|
| `src/incident_report/` (runtime modules, `web/`, `ai/`, `rules/`, `fines/`, `storage/`, `migrations/`) | tree `d4419c5…` (`src`) | `apps/incidentdesk/src/incident_report/` | Copy | The reporting app; package name kept |
| `src/incident_report/{benchmark,manifest,truth_draft}/` | inside `d4419c5…` | — | **D8** | Evaluation/judge code; needs `dataset/` |
| `tests/` | tree `6be162f…` | `apps/incidentdesk/tests/` | Copy (runtime tests); **D8** for the 25+ evaluation-coupled files | Re-run donor tests in new home |
| `samples/` | tree `6f4acbc…` | `apps/incidentdesk/samples/` | Copy | Recorded replay inputs; `sample_reports.py` expects it at app root |
| `pyproject.toml`, `uv.lock`, `.python-version` | blobs `0f3ab55…`, `5c5d32f…`, `24ee5b1…` | `apps/incidentdesk/` | Copy unchanged | Own locked env; no re-resolve |
| `scripts/check_all.*`, `scripts/guard*.py`, `check_dependencies.py`, `accept_merge.*` | tree `b47c09a…` | `apps/incidentdesk/scripts/` | Copy, then scoped re-root (reviewed) | Port safeguards |
| `scripts/` replay/measure/run_gate/run_one_pass, `export_sample_recordings.py` | same tree | — | **D8** | Evaluation tooling |
| `.pre-commit-config.yaml`, `.github/workflows/ci.yml` | `fb50fda…`, tree `6364f43…` | adapted for monorepo root | Adapt (reviewed) | CI must scope to `apps/incidentdesk/`; Actions billing is blocked anyway |
| `CODING_STANDARDS.md`, `GLOSSARY.md` | `f245279…`, `b46a1ce…` | `apps/incidentdesk/` | Copy | Reviewer rules and protected domain terms |
| `docs/adr/` (0001–0011), `THIRD_PARTY_NOTICES.md`, `.env.test.example` | inside `docs` tree `97007b9…`; `955bf95…`; `f522748…` | `apps/incidentdesk/docs/adr/`, app root | Copy | Governing decisions travel with the code |
| `specs/001-incident-video-report/` | inside `3870c03…` | `apps/incidentdesk/specs/` | Copy (reference) | Approved scope; differences recorded, not edited |
| `dataset/` (83 tracked files: manifest, truth JSON, probes; mp4s untracked) | tree `2252673…` | — | **Exclude** by default; **D8** | Handoff: exclude datasets; protected truth |
| `results/` (168 files), `output/` | tree `382e491…` | — | Exclude | Saved runs are judges, not runtime |
| `.agents/`, `.claude/`, `.Codex/`, `.agent_bridge/`, `CLAUDE.md`, `AGENTS.md` | various | — | Exclude | Donor agent tooling/logs; handoff says do not force into fork |
| `.env`, `.env.test`, `.venv`, caches | untracked | — | Exclude | Credentials and local state |
| `docs/` other than ADRs | `97007b9…` | — | Exclude; link by repo + commit | History stays in donor |

History strategy (proposal): copy files in one import commit whose message records source repo, commit `347a778`, and this manifest; keep the donor repo as the history of record. Alternative `git subtree add --prefix apps/incidentdesk` keeps full history but imports 2006 files including agent logs and protected truth, so it was not chosen as default.

## 8 Shared-MVP requirement / status / owner map

Status: **I** = implemented in code, **P** = partial, **N** = not present. "Implemented" means code exists; it is not a claim of validated accuracy.

| MVP requirement (proposal) | Donor | Fork | Owner (handoff §4) | Cards |
|---|---|---|---|---|
| Case creation, owner, status | P: incidents list + runs; no case owner field | P: `cases/<id>` per video | Joint; ticket owner TBD | MIG-01, 06 |
| Roles (officer, supervisor, fleet, driver, insurer) | N: share links only | N | Joint | MIG-06 |
| Mobile evidence capture (photos, notes, statements, documents) | N (video only) | N | Joint | MIG-06 |
| Multi-source library, hashes, reviewer offsets, clock reliability | P: ≤10 views, SHA-256, no offsets | P: single video per case, hash + timestamp provenance | Joseph (host) + joint contract | MIG-02, 03, 06 |
| Timestamped AI draft with inspectable intervals | I (replay + live Gemini, citations) | — | Joseph | MIG-04 |
| Operator questions | I | — | Joseph | MIG-04 |
| Detection, tracking, track repair | — | I | Collaborator | MIG-01, 03 |
| Calibration, camera profiles | — | P: per-case calibration; no reusable profile store | Collaborator | MIG-03 |
| Bird's-eye replay beside source | P: source player only | I (Streamlit/Canvas) | Collaborator | MIG-03 |
| Movement events, incident candidates | — | I | Collaborator | MIG-02 |
| Contribution proposals (not violations) | — | I (`rule_source` forced null) | Collaborator | MIG-02, 04 |
| Review and correction, audit | I (overrides, versions, audit log) | I (immutable review revisions) | Both layers; joint semantics | MIG-02–05 |
| Disagreement shown, not averaged | P (cross-view contradictions, consistency) | — | Joseph | MIG-04 |
| Account vs evidence comparison | N | N | Joint | MIG-06 |
| Deterministic fault/penalty | I (rules, MR 178/2017 fines) — **pilot exposure is D3** | N by design | Joseph | MIG-04 |
| Unified timeline | P (storyline + review timeline) | Planned (Phase B) | Joseph — **conflicts with fork Phase B** | MIG-03 |
| PDF from approved version | P: print view only | Planned (Phase B) | Joseph | MIG-05 |
| Evidence bundle / case package | N | P: presentation export; package planned | Joseph | MIG-05 |
| OCR road context (Arabic/English, UAE signs) | N | Planned (Phase B), from older MiniVision | Collaborator | later |
| Claims checklist, official report attachment, manual statuses | N | N | Joint product owner TBD | MIG-07 |
| Case history survives reload | I (Postgres) | I (files) | Joint | MIG-02, 05 |
| Five connected screens | P: upload, incidents list, report/review | P: Streamlit tools | Joseph host | MIG-03, 05–07 |
| Evaluation of combined workflow | Own benchmark (provisional truth) | Own evaluation (locked test split) | Both | MIG-08 |

## 9 Decisions for the owners (with my default)

Handoff section 11 decisions, now with evidence. Defaults are what I would do if the owner says "go with your pick".

**Owner rulings 2026-10-30:** D1 = the fork (local branch `integration/incidentdesk`, no push). D2 = WSL Ubuntu worker, collaborator's code unchanged. D8 = evaluation code stays in the donor repo; a smaller imported test set is accepted. Implementer for MIG-01 = Codex lane (CODEX_2), Claude Opus reviews. The collaborator is busy, so her review of the reconstruction rows is deferred; nothing of hers is edited meanwhile. Card: `.agent_bridge/tasks/MIG-01-INTEGRATION-BRANCH.md`. D3–D7, D9, D10 remain open (needed by MIG-03/04, not MIG-01).

**Owner ruling 2026-10-30, D3:** fault status and fines **are shown in the pilot**. This departs from the shared MVP proposal (which defers automatic penalty totals); the collaborator must be told when available. Unchanged guardrails: every fine cites MR 178/2017 (as amended) / Decree-Law 14/2024 with item number (ADR 0006); missing or unreviewed evidence gives Cannot Determine; imported contribution outcomes (`ONE_CONTRIBUTOR`, `MULTIPLE_CONTRIBUTORS`) and producer reviews never become fault or officer approval. Driver behaviour keeps today's code behaviour: phone-use/seatbelt violations are listed with their cited fine but do not change collision Fault Status (`rules/driver.py` docstring).

| # | Decision | Evidence from MIG-00 | Default |
|---|---|---|---|
| D1 | Fork as authoritative integration repo; merge reviewers | Fork = upstream, 1 commit, no CI, no LICENSE | Yes; Joseph + collaborator review each other's areas |
| D2 | Worker platform | Native Windows: 9/11 fork test modules cannot import (3 `fcntl` sites). Linux: see section 2 | WSL Ubuntu worker on `/mnt/d`; portable locking only as a later scoped ticket |
| D3 | Pilot assessment mode | Donor rules + fines exist; proposal defers penalty totals | Contribution + advisory findings; fault colours shown as advisory with Cannot Determine; penalty totals off |
| D4 | Local/cloud policy | Fork is local-only; donor live path calls Gemini | Import never triggers Gemini; replay only until a footage-class policy is written |
| D5 | First pilot role | Donor has share links only | Single officer/investigator role first; roles in MIG-06 |
| D6 | Frontend shell | Fork: Streamlit/Canvas (starlette <1); donor: FastAPI/Jinja (starlette 1.7) | Keep both running; FastAPI app links out to Streamlit tools by case ID |
| D7 | Fixtures and licences | No fork LICENSE; ultralytics AGPL-3.0 | Public research fixtures only; licence review before any redistribution or hosting |
| **D8 (new)** | Where evaluation code and its truth/manifest live | 25+ donor tests read `dataset/`; runtime does not import eval modules | Move runtime app + runtime tests; keep `benchmark/`, `manifest/`, `truth_draft/`, their tests and `dataset/` in the donor repo and run them there. Needs owner approval because the moved test set is smaller than the donor's. |
| **D9 (new)** | Who builds the timeline / PDF / case package | Fork README Phase B plans its own; handoff assigns to Joseph | One owner (Joseph); fork's Phase B items become inputs (figures, artifacts) |
| **D10 (new)** | Shared-doc shell syntax | Fork docs are POSIX/macOS-only; donor has PS + sh | Worker docs POSIX (WSL); app docs both |

## 10 Not checked / uncertain

- Fork's Streamlit apps and Canvas were not launched; Playwright browsers not installed; reconstruction export not run.
- No real fork export bundle exists locally (`cases/` is gitignored, empty). MIG-02 needs one from the collaborator, with permission.
- The original `SHARED MVPl.docx` was not re-read; I relied on the text extraction. Its SHA-256 was re-checked on 2026-10-29 and matches `67cbc141…42ec`, so the extraction is of the same file.
- `git ls-remote` on the donor's `origin` was not re-run.
- Licence strings are inventory only; no legal licence review was done.

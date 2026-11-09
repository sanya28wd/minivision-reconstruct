# CLAUDE.md: IncidentDesk shared repo (operating contract)

This repo joins two components into one product, **IncidentDesk**: an evidence-linked, reviewed accident report built from fixed-camera reconstruction plus Gemini video analysis.

| Component | Paths | Owner | Environment |
|---|---|---|---|
| Reconstruction worker | `src/minivision_reconstruct/`, `scripts/`, `configs/`, `tests/`, `evaluation/`, `data/`, root `pyproject.toml`/`uv.lock` | Collaborator (Sanya) | Linux or macOS (uses `fcntl`; on Windows run it in WSL), ffmpeg/ffprobe on PATH |
| Report app | `apps/incidentdesk/` (its own `CLAUDE.md` holds the app guardrails) | Joseph | Any OS, Python 3.12+, PostgreSQL, ffmpeg |
| Shared | `contracts/`, `integration/`, `docs/migration/` | Joint, one writer per ticket | — |

Machine-specific paths (checkout location, venv and tool folders, shared case folder) belong in each person's gitignored `CLAUDE.local.md`; committed files use repo-relative paths only.

## Always
- **Two locks, one boundary.** Each component keeps its own `pyproject.toml` + `uv.lock` (their starlette ranges cannot coexist). They exchange **files** that follow the evidence contract; code never imports across components. Install with `uv sync --locked`; dependency changes need their own reviewed ticket.
- **The collaborator's paths change only with her review.** Joseph-side work reads her exports and runs her commands as published.
- **Reconstruction output is a proposal.** Tracks, events and contribution outcomes keep their producer, source frames, uncertainty and review status; `ONE_CONTRIBUTOR`/`MULTIPLE_CONTRIBUTORS` and producer reviews stay observations, never fault or officer approval. Missing input is its own state, never "No Incident".
- **Source time is the citation.** Findings cite view ID + source frame/time, never replay or export time. Image-space units stay non-metric.
- **Local branch, owner publishes.** Work on feature branches off `integration/incidentdesk`; the owner decides every push, PR and upstream sync (`upstream` = sanya28wd/minivision-reconstruct).
- **Real or NDA footage stays local** (Vertex zero-retention only, never a free tier). Fixtures are public research clips or recorded model responses.
- **Fixed test splits stay sealed.** The real-footage test split (`development_allowed=false`) and held-out sets run once on a frozen configuration.

## Where things are
- Migration plan and cards: `docs/migration/` (start at `MIG-00_INVENTORY_20261029.md`). The full handoff, task cards, lane status files and session handoffs live in Joseph's donor repo `josephjose-dev/RTA-ACCIDENT-AGENT` (`docs/MINIVISION_INCIDENTDESK_CLAUDE_MIGRATION_HANDOFF_20261029.md`, `.agent_bridge/`, `.claude/sessions/`).
- Starting either component: `docs/migration/START.md`.
- Reconstruction science rules (no speed from video, bottom-centre positions, limitations): the fork `README.md` and `IMPLEMENTATION_PLAN.md`.
- **Codex: yes.** Codex lanes implement; Claude reviews and re-runs every check. The reviewer is never the implementer.

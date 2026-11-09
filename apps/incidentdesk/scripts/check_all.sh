#!/usr/bin/env bash
# Run every CI check, in the CI order, and report every check failure (docs/CI_GUARDRAILS.md).
#
#   scripts/check_all.sh [--base REF] [--head REF]
#
# --base defaults to incidentdesk-import-approved (owner-created annotated tag); override it with --base or BASE=... in the
# environment. Test settings come from an optional, gitignored .env.test (see .env.test.example):
# PGPASSWORD, TEST_DATABASE_URL, INCIDENT_PG_BIN_DIR. Anything already set in your shell wins over the file.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BASE="${BASE:-incidentdesk-import-approved}"
HEAD_REF="HEAD"
while [ $# -gt 0 ]; do
  case "$1" in
    --base | --Base) BASE="${2:?--base needs a ref}"; shift 2 ;;
    --head) HEAD_REF="${2:?--head needs a ref}"; shift 2 ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# A worktree shares the main checkout's optional settings when it has no local file.
settings_file=.env.test
if [ ! -f "$settings_file" ]; then
  main_checkout="$(git -c core.quotePath=false worktree list --porcelain | sed -n '1s/^worktree //p')"
  if [ -d "$main_checkout/apps/incidentdesk" ]; then
    settings_file="$main_checkout/apps/incidentdesk/.env.test"
  else
    settings_file="$main_checkout/.env.test"
  fi
fi
# Read only the three known keys from .env.test (the file is data, never executed).
if [ -f "$settings_file" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      PGPASSWORD=* | TEST_DATABASE_URL=* | INCIDENT_PG_BIN_DIR=*)
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%\"}"; value="${value#\"}"
        if [ -z "${!key:-}" ]; then export "$key=$value"; fi
        ;;
    esac
  done < "$settings_file"
fi
# Local test admin connection on port 5433; used only to create throwaway test databases.
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql://postgres@localhost:5433/postgres}"
if [ -n "${INCIDENT_PG_BIN_DIR:-}" ]; then export INCIDENT_PG_BIN_DIR; fi

passed=0
failed=0
uv run python scripts/gate_base.py --base "$BASE" --head "$HEAD_REF"
run() {
  local name="$1"
  shift
  echo
  echo "=== $name ==="
  if "$@"; then
    passed=$((passed + 1))
  else
    echo
    echo "FAILED: $name"
    echo "check_all: $passed passed, then FAILED at: $name"
    failed=$((failed + 1))
  fi
}

guard() { uv run python "scripts/$1.py" "${@:2}"; }

run "ruff format" uv run ruff format --check src tests scripts
run "ruff check" uv run ruff check src tests scripts
run "mypy" uv run mypy
run "pytest" uv run pytest -q -rfE
run "collaborator paths unchanged" guard collaborator_paths --base "$BASE" --head "$HEAD_REF"
run "guard_conflict_markers" guard guard_conflict_markers --base "$BASE" --head "$HEAD_REF"
run "guard_utf8" guard guard_utf8 --base "$BASE" --head "$HEAD_REF"
run "guard_protected_paths" guard guard_protected_paths --base "$BASE" --head "$HEAD_REF"
run "guard_test_integrity" guard guard_test_integrity --base "$BASE" --head "$HEAD_REF"
run "guard_secrets" guard guard_secrets --base "$BASE" --head "$HEAD_REF"
run "check_dependencies" guard check_dependencies --offline --base "$BASE" --head "$HEAD_REF"
run "guard_evidence_defaults" guard guard_evidence_defaults --base "$BASE" --head "$HEAD_REF"
run "guard_migration_immutability" guard guard_migration_immutability --base "$BASE" --head "$HEAD_REF"

echo
if [ "$failed" -ne 0 ]; then
  echo "check_all: $passed/13 passed, $failed failed (base: $BASE)"
  exit 1
fi
echo "check_all: all $passed/13 checks passed (base: $BASE)"

#!/bin/sh
# Run in WSL, with the collaborator's existing environment and local case/export roots.
set -eu
case "${INCIDENTDESK_NATIVE_TOOLS:-false}" in false|0|'') echo 'Native tools are disabled' >&2; exit 2;; esac
: "${RECONSTRUCTION_PYTHON:?Set the collaborator venv Python}"
native_flag=$("$RECONSTRUCTION_PYTHON" -c 'import os; print(os.environ.get("INCIDENTDESK_NATIVE_TOOLS", "false").strip().lower())')
case "$native_flag" in true|1) ;; *) echo 'Native tools are disabled' >&2; exit 2;; esac
: "${INCIDENTDESK_CASES_ROOT:?Set the local shared cases root}"
: "${INCIDENTDESK_EVIDENCE_EXPORTS_ROOT:?Set the local shared evidence exports root}"
: "${INCIDENTDESK_TOOLS_TOKEN:?Set the same local token in the app and worker environment}"
for root in "$INCIDENTDESK_CASES_ROOT" "$INCIDENTDESK_EVIDENCE_EXPORTS_ROOT"; do
  case "$root" in /mnt/[a-z]/*) ;; *) echo 'Tool roots must be absolute WSL paths on a local Windows drive' >&2; exit 2;; esac
done
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
export PYTHONPATH="$PWD:$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$RECONSTRUCTION_PYTHON" -m integration.tools_worker

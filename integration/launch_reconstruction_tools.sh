#!/bin/sh
# Run in WSL. These are the collaborator's unchanged entry points and flags.
set -eu
case "${INCIDENTDESK_RECONSTRUCTION:-false}" in true|1) ;; *) echo 'Reconstruction workspace is disabled' >&2; exit 2;; esac
: "${INCIDENTDESK_CASES_ROOT:?Set INCIDENTDESK_CASES_ROOT to the shared local cases root}"
: "${RECONSTRUCTION_PYTHON:?Set RECONSTRUCTION_PYTHON to the collaborator venv python}"
case "$INCIDENTDESK_CASES_ROOT" in /*) ;; *) echo 'cases root must be an absolute WSL path' >&2; exit 2;; esac
case "$INCIDENTDESK_CASES_ROOT" in /mnt/[a-z]/*) ;; *) echo 'cases root must be on a local Windows drive' >&2; exit 2;; esac
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
pids=''
cleanup() { for pid in $pids; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
launch() {
  script=$1; tool=$2; port=$3; shift 3
  "$RECONSTRUCTION_PYTHON" -m streamlit run "$script" \
    --server.baseUrlPath "reconstruct/$tool" --server.port "$port" \
    --server.address 127.0.0.1 --server.headless true \
    --browser.gatherUsageStats false -- --cases-root "$INCIDENTDESK_CASES_ROOT" "$@" &
  pids="$pids $!"
}
launch scripts/review_tracks.py tracks 8511
launch scripts/calibrate_case.py calibration 8512
# Chromium is needed for export, not to open the workspace; do not install it here.
launch scripts/reconstruct_case.py replay 8513 --media-port 8766 --ffmpeg "${RECONSTRUCTION_FFMPEG:-ffmpeg}"
wait

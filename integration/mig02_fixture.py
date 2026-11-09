"""Package MIG-02 synthetic producer JSON artifacts without changing their bytes.

Run after the WSL generation/review commands in fixtures/README.md.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT.parent / "cases"
OUT = ROOT / "contracts/evidence-v1/fixtures"
for case in sorted(CASES.glob("mig02-*")):
    if not case.is_dir():
        continue
    target = OUT / case.name
    for path in sorted(case.rglob("*")):
        if path.suffix in {".json", ".jsonl"} and "annotations" not in path.parts:
            destination = target / case.name / path.relative_to(case)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    rec_run = "week8-incident-relative" if case.name == "mig02-relative" else "week8-incident"
    rec_path = f"{case.name}/analysis/reconstruction/{rec_run}/manifest.json"
    movement_path = f"{case.name}/analysis/movement/mig02-movement/manifest.json"
    incident_path = f"{case.name}/analysis/incident/mig02-incident/manifest.json"
    rec = json.loads((target / rec_path).read_bytes())
    incident = target / case.name / "analysis/incident/mig02-incident"
    revision = sorted(p.name for p in (incident / "reviews").glob("revision-*"))[-1] if (incident / "reviews").exists() else "initial"
    envelope = {
        "contract_version": "1.0", "producer": "minivision-reconstruct",
        "producer_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "case_id": case.name, "created_at": "2026-10-30T00:00:00Z", "processing_policy": "local_only",
        "config_identifiers": [json.loads((target / movement_path).read_bytes())["config_id"], json.loads((target / incident_path).read_bytes())["config_id"]],
        "model_identifiers": ["scripted-tracks-no-detector"],
        "artifacts": {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(target.rglob("*")) if p.is_file() and p.name != "bundle.json"},
        "sources": [{
            "view_id": "fixed-camera-1", "original_video_sha256": rec["source_video_sha256"],
            "reconstruction_manifest": rec_path, "movement_manifest": movement_path, "incident_manifest": incident_path,
            "reviewed_manifest": f"{case.name}/analysis/incident/mig02-incident/reviewed/{revision}/manifest.json",
            "tracking_run": rec["tracking_run"], "track_revision": rec["track_revision"],
            "calibration_revision": rec["calibration_revision"], "reconstruction_run": rec["run_id"],
            "movement_run": "mig02-movement", "incident_run": "mig02-incident", "review_revision": revision,
            "video_metadata": json.loads((case / "metadata/video_metadata.json").read_bytes()),
            "transformations": [{"operation": "producer reconstruction replay", "mapping": rec_path.replace("manifest.json", "replay_frames.jsonl")}],
            "frame_origin": 0,
        }],
    }
    (target / "bundle.json").write_text(json.dumps(envelope, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(case.name, len(envelope["artifacts"]))

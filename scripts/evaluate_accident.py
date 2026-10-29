"""Week 8 ACCIDENT synthetic benchmark: run one split through the full chain, then write the §11.2 result table.

`prepare` ingests each clip, tracks it, writes a calibration from the CARLA camera pose, and runs reconstruction,
movement, and incident analysis with the given (frozen) configs. Finished steps are skipped, so a stopped run resumes.
`evaluate` scores the incident run against the manifest labels and refuses to overwrite an existing output folder.
"""

import argparse
import csv
from dataclasses import replace
import json
from pathlib import Path
import shutil

import cv2

from minivision_reconstruct.calibration import save_calibration_revision
from minivision_reconstruct.evidence.case_store import ingest_case
from minivision_reconstruct.incident.accident_benchmark import FAILURE_SUFFIX, CameraModel, calibration_assumptions, carla_calibration, evaluate_accident
from minivision_reconstruct.incident.models import IncidentError
from minivision_reconstruct.incident.pipeline import analyze_incident, load_incident_config
from minivision_reconstruct.movement.pipeline import analyze_movement
from minivision_reconstruct.domain.models import DetectorConfiguration, TrackerConfiguration
from minivision_reconstruct.perception.configuration import load_detector_configuration
from minivision_reconstruct.perception.tracking_configuration import load_tracker_configuration
from minivision_reconstruct.perception.tracking_workflow import run_tracking_case
from minivision_reconstruct.reconstruction.pipeline import automatic_track_input, prepare_reconstruction

CAMERA = CameraModel(fov_degrees=90.0, ground_z=0.0, max_range=150.0)


def _annotation(data_root: Path, row: dict[str, str]) -> Path:
    path = data_root / row["annotations_path"]
    return path if path.is_file() else path.with_suffix("")


def prepare(args: argparse.Namespace) -> None:
    with args.selection.open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.DictReader(stream) if row["split"] == args.split]
    detector, tracker = load_detector_configuration(args.detector_config), load_tracker_configuration(args.tracker_config)
    cases_root = args.cases_root.resolve()
    for number, row in enumerate(rows, 1):
        case_id = f"{args.case_prefix}{row['local_video_id']}"
        case = cases_root / case_id
        print(f"[{number}/{len(rows)}] {case_id}", flush=True)
        failure = case / "analysis" / "incident" / f"{args.incident_run}{FAILURE_SUFFIX}"
        if failure.exists():
            continue
        try:
            run_clip(args, row, cases_root, case_id, detector, tracker)
        except Exception as error:  # one clip's failure is a result to report, not a reason to stop the split
            failure.parent.mkdir(parents=True, exist_ok=True)
            failure.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
            print(f"  failed: {type(error).__name__}: {error}", flush=True)


def run_clip(args: argparse.Namespace, row: dict[str, str], cases_root: Path, case_id: str, detector: DetectorConfiguration, tracker: TrackerConfiguration) -> None:
    case = cases_root / case_id
    if not case.exists():
        ingest_case(args.data_root / row["rgb_path"], cases_root, case_id, args.ffprobe)
    if not (case / "analysis" / "tracks" / args.tracking_run).exists():
        run_tracking_case(cases_root, case_id, args.tracking_run, detector, tracker)
    calibration_path = case / "analysis" / "calibration" / "revision-001" / "calibration.json"
    if not calibration_path.exists():
        evidence = json.loads((case / "metadata" / "evidence_manifest.json").read_text(encoding="utf-8"))
        calibration = carla_calibration(_annotation(args.data_root, row), case_id, evidence["preserved_sha256"],
                                        evidence["width"], evidence["height"], CAMERA)
        capture = cv2.VideoCapture(str(case / "original" / Path(row["rgb_path"]).name))
        ok, frame = capture.read()
        capture.release()
        if not ok:
            raise IncidentError(f"Could not read the first frame of {case_id}.")
        revision = save_calibration_revision(case, calibration, frame)
        (revision.parent / f"ASSUMPTIONS-{revision.name}.md").write_text(
            calibration_assumptions(_annotation(args.data_root, row), CAMERA, replace(calibration, calibration_id=revision.name)),
            encoding="utf-8")
    if not (case / "analysis" / "reconstruction" / args.reconstruction_run).exists():
        tracks = automatic_track_input(cases_root, case_id, args.tracking_run)
        prepare_reconstruction(cases_root, case_id, tracks, calibration_path, args.reconstruction_run, args.ffmpeg)
    if not (case / "analysis" / "movement" / args.movement_run).exists():
        analyze_movement(cases_root, case_id, args.reconstruction_run, args.movement_config, args.movement_run)
    if not (case / "analysis" / "incident" / args.incident_run).exists():
        analyze_incident(cases_root, case_id, args.movement_run, args.incident_config, args.incident_run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--selection", type=Path, required=True)
        command.add_argument("--split", choices=("week8_dev", "week8_heldout"), required=True)
        command.add_argument("--data-root", type=Path, required=True, help="Folder that contains synthetic_videos/ from the Kaggle download.")
        command.add_argument("--cases-root", type=Path, required=True)
        command.add_argument("--case-prefix", default="accident-syn-")
        command.add_argument("--incident-run", required=True)
        command.add_argument("--incident-config", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--tracking-run", required=True)
            command.add_argument("--reconstruction-run", required=True)
            command.add_argument("--movement-run", required=True)
            command.add_argument("--movement-config", type=Path, required=True)
            command.add_argument("--detector-config", type=Path, required=True)
            command.add_argument("--tracker-config", type=Path, required=True)
            command.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
            command.add_argument("--ffprobe", default=shutil.which("ffprobe"))
        else:
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        output = evaluate_accident(args.selection, args.split, args.cases_root.resolve(), args.data_root, args.incident_run,
                                   load_incident_config(args.incident_config), args.case_prefix, args.output)
        print((output / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

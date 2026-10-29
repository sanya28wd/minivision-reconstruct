"""Prepare a Week 6 replay from explicitly selected tracking and calibration inputs."""

import argparse
from pathlib import Path

from minivision_reconstruct.reconstruction.pipeline import automatic_track_input, prepare_reconstruction, reviewed_track_input


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--tracking-run", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--automatic", action="store_true")
    source.add_argument("--review-revision")
    parser.add_argument("--calibration-revision", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ffmpeg", required=True)
    args = parser.parse_args()
    cases_root = args.cases_root.resolve()
    tracks = automatic_track_input(cases_root, args.case_id, args.tracking_run) if args.automatic else reviewed_track_input(cases_root, args.case_id, args.tracking_run, args.review_revision)
    calibration = cases_root / args.case_id / "analysis" / "calibration" / args.calibration_revision / "calibration.json"
    destination = prepare_reconstruction(cases_root, args.case_id, tracks, calibration, args.run_id, args.ffmpeg)
    print(destination)


if __name__ == "__main__":
    main()

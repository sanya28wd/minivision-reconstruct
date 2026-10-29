"""Run explicit all-frame ByteTrack processing for one evidence case."""

import argparse
from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.perception.configuration import load_detector_configuration
from minivision_reconstruct.perception.tracking_configuration import load_tracker_configuration
from minivision_reconstruct.perception.tracking_workflow import run_tracking_case


def main(arguments: list[str]) -> int:
    """Execute one non-overwriting tracking run."""
    parser = argparse.ArgumentParser(description="Track road users in every frame of an evidence case.")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--cases-root", required=True, type=Path)
    parser.add_argument("--detector-config", required=True, type=Path)
    parser.add_argument("--tracker-config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parsed = parser.parse_args(arguments)
    try:
        result = run_tracking_case(parsed.cases_root, parsed.case_id, parsed.run_id, load_detector_configuration(parsed.detector_config), load_tracker_configuration(parsed.tracker_config))
    except EvidenceError as error:
        print(f"Tracking failed: {error}", file=sys.stderr)
        return 1
    print(f"Run '{parsed.run_id}' complete: {result.total_frames} frames, {result.total_tracks} tracks; records {result.track_records_path}; video {result.tracking_video_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

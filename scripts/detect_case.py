"""Non-interactive Week 2 road-user detection command."""

import argparse
from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.perception.configuration import load_detector_configuration
from minivision_reconstruct.perception.workflow import run_detection_case


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Parse all explicit inputs for one detection run."""
    parser = argparse.ArgumentParser(description="Run configured independent road-user detection for a Week 1 case.")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--cases-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    """Run detection and print a privacy-conscious concise summary."""
    parsed = parse_arguments(arguments)
    try:
        configuration = load_detector_configuration(parsed.config)
        result = run_detection_case(parsed.cases_root, parsed.case_id, parsed.run_id, configuration)
    except EvidenceError as error:
        print(f"Detection failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Run '{parsed.run_id}' complete for case '{parsed.case_id}': {result.total_frames} frames, "
        f"{result.frames_with_detections} with detections, {result.total_detections} total; "
        f"model {result.provenance.model_name} ({result.provenance.library_version}) on {result.provenance.device}; "
        f"latency mean/median/p95 {result.mean_latency_seconds:.4f}/{result.median_latency_seconds:.4f}/{result.p95_latency_seconds:.4f}s; "
        f"records {result.detection_records_path}; video {result.annotated_video_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Non-interactive command line entrypoint for Week 1 evidence ingestion."""

import argparse
from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.evidence.case_store import ingest_case


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Parse explicit evidence-ingestion command-line arguments."""
    parser = argparse.ArgumentParser(description="Preserve and validate a video evidence case.")
    parser.add_argument("--input", required=True, type=Path, help="Path to the source video file.")
    parser.add_argument("--case-id", required=True, help="Safe, unique case identifier.")
    parser.add_argument("--cases-root", required=True, type=Path, help="Directory containing case folders.")
    parser.add_argument("--ffprobe", required=True, help="Path or command name for ffprobe.")
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    """Run ingestion and return a process exit status."""
    parsed = parse_arguments(arguments)
    try:
        result = ingest_case(parsed.input, parsed.cases_root, parsed.case_id, parsed.ffprobe)
    except EvidenceError as error:
        print(f"Evidence ingestion failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Case '{result.case_id}' ingested: {result.manifest.decoded_frame_count} decoded frames; "
        f"SHA-256 {result.manifest.preserved_sha256}; manifest {result.manifest_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

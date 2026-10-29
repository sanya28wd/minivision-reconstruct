"""Export a selected immutable reconstruction revision through the shared renderer."""

import argparse
from pathlib import Path

from minivision_reconstruct.reconstruction.export import export_reconstruction
from minivision_reconstruct.reconstruction.media import ReplayMediaConnector
from minivision_reconstruct.reconstruction.models import require_identifier


def report_progress(done: int, total: int) -> None:
    if done % 30 == 0 or done == total:
        print(f"Rendered {done}/{total} frames", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scene-revision", required=True)
    parser.add_argument("--marker-revision", required=True)
    parser.add_argument("--export-id", required=True)
    parser.add_argument("--snapshot-frame", type=int, required=True)
    parser.add_argument("--media-port", type=int, required=True)
    parser.add_argument("--ffmpeg", required=True)
    args = parser.parse_args()
    for name in ("case_id", "run_id", "export_id"):
        require_identifier(getattr(args, name), name)
    root = args.cases_root.resolve()
    run = root / args.case_id / "analysis" / "reconstruction" / args.run_id
    media = ReplayMediaConnector(root, run, args.scene_revision, args.marker_revision, args.media_port)
    media.start()
    try:
        destination = export_reconstruction(media, run / "exports" / args.export_id, args.scene_revision, args.marker_revision, args.snapshot_frame, args.ffmpeg, report_progress)
        print(destination)
    finally:
        media.close()


if __name__ == "__main__":
    main()

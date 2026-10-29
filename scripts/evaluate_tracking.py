"""Export finalized tracks in MOT text format for TrackEval-compatible evaluation."""

import argparse
import json
from pathlib import Path
import sys


def main(arguments: list[str]) -> int:
    """Convert scored track states into per-sequence MOT tracker files."""
    parser = argparse.ArgumentParser(description="Export observed ByteTrack records for TrackEval.")
    parser.add_argument("--tracks-jsonl", required=True, type=Path)
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--iou-threshold", required=True, type=float)
    parsed = parser.parse_args(arguments)
    if not 0 < parsed.iou_threshold <= 1:
        print("Evaluation export failed: iou threshold must be in (0, 1].", file=sys.stderr)
        return 1
    if not parsed.tracks_jsonl.is_file():
        print(f"Evaluation export failed: missing tracks file '{parsed.tracks_jsonl}'.", file=sys.stderr)
        return 1
    output = parsed.output_root / "data" / f"{parsed.sequence}.txt"
    if output.exists():
        print(f"Evaluation export failed: refusing to overwrite '{output}'.", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    with parsed.tracks_jsonl.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            value = json.loads(line)
            if value["state"] not in {"observed", "recovered"}:
                continue
            box = (value["bbox_x1"], value["bbox_y1"], value["bbox_x2"], value["bbox_y2"])
            if any(item is None for item in box):
                print(f"Evaluation export failed: scored record at line {line_number} has no box.", file=sys.stderr)
                return 1
            x1, y1, x2, y2 = (float(item) for item in box)
            rows.append(f"{int(value['frame_number']) + 1},{int(value['track_id'])},{x1:.6f},{y1:.6f},{x2 - x1:.6f},{y2 - y1:.6f},{float(value['confidence']):.6f},1,1,1")
    output.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    metadata = {"sequence": parsed.sequence, "tracker_result": str(output), "iou_threshold": parsed.iou_threshold, "scored_states": ["observed", "recovered"], "excluded_states": ["lost", "interpolated"], "next_step": "Run TrackEval against this MOT-format tracker output and prepared gt/gt.txt files."}
    (parsed.output_root / "export_manifest.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Exported {len(rows)} scored track rows to '{output}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

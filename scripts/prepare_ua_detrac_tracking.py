"""Validate fixed UA-DETRAC sequences and convert official XML tracks to MOT rows."""

import argparse
import csv
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as element_tree


TUNING_SEQUENCES = ("MVI_39781", "MVI_40732")
EVALUATION_SEQUENCES = ("MVI_39851", "MVI_40181", "MVI_40213", "MVI_41073", "MVI_63552", "MVI_63561")


def main(arguments: list[str]) -> int:
    """Convert supplied official UA-DETRAC annotations without downloading data implicitly."""
    parser = argparse.ArgumentParser(description="Prepare the fixed official UA-DETRAC tracking subset.")
    parser.add_argument("--frames-root", required=True, type=Path)
    parser.add_argument("--annotations-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    if parsed.output_root.exists():
        print(f"Preparation failed: output root already exists: '{parsed.output_root}'.", file=sys.stderr)
        return 1
    try:
        prepared = [_prepare_sequence(sequence, split, parsed.frames_root, parsed.annotations_root, parsed.output_root) for split, sequences in (("tuning", TUNING_SEQUENCES), ("evaluation", EVALUATION_SEQUENCES)) for sequence in sequences]
        _write_manifest(parsed.output_root, prepared)
        _append_registry(parsed.registry, parsed.frames_root, parsed.annotations_root)
    except (OSError, ValueError, element_tree.ParseError) as error:
        print(f"Preparation failed: {error}", file=sys.stderr)
        return 1
    print(f"Prepared {len(prepared)} UA-DETRAC tracking sequences in '{parsed.output_root}'.")
    return 0


def _prepare_sequence(sequence: str, split: str, frames_root: Path, annotations_root: Path, output_root: Path) -> dict[str, object]:
    image_directory = frames_root / sequence
    annotation_path = annotations_root / f"{sequence}.xml"
    images = tuple(sorted(image_directory.glob("*.jpg")))
    if not images:
        raise ValueError(f"No JPEG frames found for '{sequence}' at '{image_directory}'.")
    if not annotation_path.is_file():
        raise ValueError(f"Missing official XML annotation for '{sequence}' at '{annotation_path}'.")
    root = element_tree.parse(annotation_path).getroot()
    rows: list[str] = []
    attributes: list[dict[str, object]] = []
    seen_frame_numbers: list[int] = []
    for frame in root.findall(".//frame"):
        frame_number = int(frame.attrib["num"])
        seen_frame_numbers.append(frame_number)
        for target in frame.findall("./target_list/target"):
            target_id = int(target.attrib["id"])
            box = target.find("box")
            if box is None:
                raise ValueError(f"Missing box for '{sequence}', frame {frame_number}, target {target_id}.")
            left, top, width, height = (float(box.attrib[name]) for name in ("left", "top", "width", "height"))
            if width <= 0 or height <= 0:
                raise ValueError(f"Invalid box for '{sequence}', frame {frame_number}, target {target_id}.")
            attribute = target.find("attribute")
            details = attribute.attrib if attribute is not None else {}
            rows.append(f"{frame_number},{target_id},{left:.6f},{top:.6f},{width:.6f},{height:.6f},1,1,1,1")
            attributes.append({"frame_number": frame_number, "target_id": target_id, "vehicle_type": details.get("vehicle_type"), "occlusion_status": details.get("occlusion_status"), "truncation_ratio": details.get("truncation_ratio")})
    if not rows:
        raise ValueError(f"No target annotations found in '{annotation_path}'.")
    if len(set(seen_frame_numbers)) != len(seen_frame_numbers):
        raise ValueError(f"Duplicate frame annotation in '{annotation_path}'.")
    if max(seen_frame_numbers) > len(images):
        raise ValueError(f"Annotation frame count exceeds available images for '{sequence}'.")
    sequence_directory = output_root / split / sequence
    gt_directory = sequence_directory / "gt"
    gt_directory.mkdir(parents=True, exist_ok=False)
    (gt_directory / "gt.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (sequence_directory / "attributes.jsonl").write_text("".join(json.dumps(value, sort_keys=True) + "\n" for value in attributes), encoding="utf-8")
    return {"sequence": sequence, "split": split, "image_directory": str(image_directory), "annotation_path": str(annotation_path), "frames_available": len(images), "annotated_frames": len(seen_frame_numbers), "ground_truth_boxes": len(rows)}


def _write_manifest(output_root: Path, sequences: list[dict[str, object]]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "subset_manifest.json").write_text(json.dumps({"dataset": "UA-DETRAC official training data", "tuning_sequences": list(TUNING_SEQUENCES), "evaluation_sequences": list(EVALUATION_SEQUENCES), "sequences": sequences}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_registry(path: Path, frames_root: Path, annotations_root: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Registry does not exist: '{path}'.")
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["UA-DETRAC", "official-training-tracking", "https://detrac-db.rit.albany.edu/", "Verify official dataset terms before use", "manual", "week3-fixed-subset", "8-sequences", "tuning-and-held-out-evaluation", "true", "true", f"frames={frames_root};annotations={annotations_root}", "", "", "", "", "", "", "official XML trajectories converted to MOT rows", "Research benchmark; no cross-camera re-identification"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

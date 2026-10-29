"""Stage prepared UA-DETRAC ground truth and tracker rows for TrackEval."""

import argparse
import json
from pathlib import Path
import shutil
import sys


def main(arguments: list[str]) -> int:
    """Create an immutable MOTChallenge-style TrackEval input tree."""
    parser = argparse.ArgumentParser(description="Stage UA-DETRAC tracks and GT for TrackEval.")
    parser.add_argument("--ground-truth-root", required=True, type=Path)
    parser.add_argument("--mot-root", required=True, type=Path)
    parser.add_argument("--sequence-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--tracker-name", required=True)
    parser.add_argument("--split", choices=("tuning", "evaluation"))
    parsed = parser.parse_args(arguments)
    if parsed.output_root.exists():
        print(f"Staging failed: refusing to overwrite '{parsed.output_root}'.", file=sys.stderr)
        return 1
    try:
        _stage(parsed.ground_truth_root, parsed.mot_root, parsed.sequence_root, parsed.output_root, parsed.tracker_name, parsed.split)
    except (OSError, ValueError) as error:
        print(f"Staging failed: {error}", file=sys.stderr)
        return 1
    print(f"TrackEval input staged in '{parsed.output_root}'.")
    return 0


def _stage(
    ground_truth_root: Path, mot_root: Path, sequence_root: Path, output_root: Path, tracker_name: str, requested_split: str | None,
) -> None:
    splits = (requested_split,) if requested_split else tuple(sorted(path.name for path in ground_truth_root.iterdir() if path.is_dir()))
    if not splits:
        raise ValueError(f"No split directories found in '{ground_truth_root}'.")
    for split in splits:
        if not (ground_truth_root / split).is_dir():
            raise ValueError(f"Requested split '{split}' does not exist in '{ground_truth_root}'.")
        sequence_names = tuple(sorted(path.name for path in (ground_truth_root / split).iterdir() if path.is_dir()))
        if not sequence_names:
            raise ValueError(f"No sequences found in '{ground_truth_root / split}'.")
        benchmark = f"UADETRAC-{split}"
        _write_seqmap(output_root / "gt" / "mot_challenge" / "seqmaps" / f"{benchmark}.txt", sequence_names)
        for sequence in sequence_names:
            source_gt = ground_truth_root / split / sequence / "gt" / "gt.txt"
            source_tracks = mot_root / sequence / "data" / f"{sequence}.txt"
            manifest = sequence_root / split / sequence / "sequence_manifest.json"
            if not source_gt.is_file() or not source_tracks.is_file() or not manifest.is_file():
                raise ValueError(f"Missing GT, MOT rows, or sequence manifest for '{split}/{sequence}'.")
            metadata: dict[str, object] = json.loads(manifest.read_text(encoding="utf-8"))
            frames = int(metadata["frames"])
            gt_directory = output_root / "gt" / "mot_challenge" / benchmark / sequence / "gt"
            tracker_directory = output_root / "trackers" / "mot_challenge" / benchmark / tracker_name / "data"
            gt_directory.mkdir(parents=True, exist_ok=False)
            tracker_directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_gt, gt_directory / "gt.txt")
            shutil.copyfile(source_tracks, tracker_directory / f"{sequence}.txt")
            _write_seqinfo(gt_directory.parent / "seqinfo.ini", sequence, frames)


def _write_seqmap(path: Path, sequence_names: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name\n" + "\n".join(sequence_names) + "\n", encoding="utf-8")


def _write_seqinfo(path: Path, sequence: str, frames: int) -> None:
    path.write_text(
        "[Sequence]\n"
        f"name={sequence}\n"
        "imDir=img1\n"
        "frameRate=25\n"
        f"seqLength={frames}\n"
        "imWidth=960\n"
        "imHeight=540\n"
        "imExt=.jpg\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Offline crash-finder experiment. Only this harness reads contact-time truth.

No API calls. Method A is a fine-grid FFmpeg frame-difference fallback. All
thresholds are project assumptions; this small corpus is not an accuracy claim.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from incident_report.ai import crash_finder_motion as motion

CLIPS = ("MODERATE_04", "MODERATE_05", "MINOR_01", "VIDEO1", "video3", "night1", "MAJOR_06", "day1")
HIT_SECONDS = 1.5  # owner-assigned measurement tolerance, spec 003 / lane prompt


def _contact_times(root: Path, clip: str) -> list[float]:
    data = json.loads((root / "dataset/truth/gemini_dense" / f"{clip}.json").read_text(encoding="utf-8"))
    return [
        sum(float(part) * 60**index for index, part in enumerate(reversed(str(c["timestamp"]).split(":"))))
        for c in data["truth"]["collisions"]
    ]


def _stills(video: Path, candidate: motion.ContactCandidate, target: Path) -> None:
    x1, y1, x2, y2 = candidate.area
    subprocess.run(  # noqa: S603 fixed executable/options; shell disabled; archive path
        [  # noqa: S607 installed FFmpeg CLI, fixed executable
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-noautorotate",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            f"setpts=PTS-STARTPTS,select=gte(t\\,{candidate.timestamp}),crop={x2 - x1}:{y2 - y1}:{x1}:{y1}",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=motion.TIMEOUT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("a",), required=True)
    parser.add_argument(
        "--root", type=Path, default=Path("."), help="Read-only corpus checkout; no output written here."
    )
    parser.add_argument("--output", type=Path, default=Path("results/crash_finder/a"))
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    config = {name: getattr(motion, name) for name in vars(motion) if name.isupper()}
    version = subprocess.run(  # noqa: S603 fixed command
        ["ffmpeg", "-version"],  # noqa: S607 installed FFmpeg CLI
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.splitlines()[0]
    print(
        json.dumps(
            {
                "method": "a",
                "implementation": "ffmpeg_grid_frame_difference",
                "ffmpeg": version,
                "configuration_project_assumptions": config,
                "truth_tolerance_seconds": HIT_SECONDS,
            }
        ),
        flush=True,
    )
    rows: list[dict[str, Any]] = []
    failed = False
    for clip in CLIPS:
        started = time.perf_counter()
        try:
            videos = [p for p in (root / "dataset/archive/videos").rglob("*") if p.is_file() and p.stem == clip]
            if len(videos) != 1:
                raise ValueError(f"Expected exactly one archive video for {clip}, found {len(videos)}")
            video = videos[0]
            # No truth is supplied to the runtime module; scoring happens after detection.
            detected = motion.find_contact_candidates(video)
            elapsed = time.perf_counter() - started
            truth = _contact_times(root, clip)
            hits = [any(abs(c.timestamp - t) <= HIT_SECONDS for c in detected.candidates) for t in truth]
            false = [
                c.timestamp for c in detected.candidates if not any(abs(c.timestamp - t) <= HIT_SECONDS for t in truth)
            ]
            stills = []
            for index, candidate in enumerate(detected.candidates):
                target = args.output / f"{clip}_{index:02d}_{candidate.timestamp:.3f}.png"
                _stills(video, candidate, target)
                stills.append(str(target))
            row = {
                "clip": clip,
                "candidates": [asdict(c) for c in detected.candidates],
                "truth_contact_seconds": truth,
                "contact_hits": hits,
                "hit": all(hits) if truth else None,
                "false_candidates": false,
                "runtime_seconds": round(elapsed, 6),
                "audio_status": detected.audio_status,
                "scene_cuts": detected.scene_cuts,
                "camera_motion_count": len(detected.camera_motion),
                "cropped_stills": stills,
                "area_correct": "requires visual review; no spatial truth annotations",
            }
        except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
            failed = True
            row = {"clip": clip, "error": str(exc), "runtime_seconds": round(time.perf_counter() - started, 6)}
        rows.append(row)
        print(json.dumps(row), flush=True)
    (args.output / "measurement.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

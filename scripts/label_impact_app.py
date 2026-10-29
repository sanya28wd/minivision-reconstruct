"""Label the colliding road users around each impact of real ACCIDENT clips (Streamlit).

PYTHONPATH=src .week6-venv/bin/python -m streamlit run scripts/label_impact_app.py -- \\
  --selection evaluation/real-footage/selection.csv --split real_dev --per-type 4 \\
  --work data/accident/real/impact-label-work/dev --data-root data/accident/real \\
  --labels evaluation/real-footage/impact-labels/real_dev

For every keyframe, pick which numbered box is road user A and which is B (only A for a single-vehicle crash),
choose "not visible" when it cannot be seen, or type a box when no proposal fits. The suggestion is only a
starting point; saving records your name as the annotator.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

import cv2
import streamlit as st

from label_impact_tracks import ROAD_USER_CLASSES, chosen_clips, draw_panel, read_frame
from minivision_reconstruct.incident.impact_labels import ImpactLabel, label_from_choices, label_path, suggest_choices

NOT_VISIBLE, MANUAL = "not visible", "type a box"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--split", choices=("real_dev", "real_test"), required=True)
    parser.add_argument("--per-type", type=int, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    return parser.parse_args(sys.argv[1:])


@st.cache_data(show_spinner=False)
def _frame(video: str, frame: int):
    return cv2.cvtColor(read_frame(Path(video), frame), cv2.COLOR_BGR2RGB)


def _preview(video: Path, start: float, destination: Path) -> Path:
    """A short H.264 clip around the impact so the labeller can watch what happened."""
    if not destination.exists():
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{max(start, 0):.2f}", "-t", "6", "-i", str(video), "-an",
                        "-vf", "scale=-2:540,format=yuv420p", "-c:v", "libx264", "-crf", "26", str(destination)], check=True)
    return destination


def _existing_choices(label: ImpactLabel, proposals: dict[int, list[list[float]]]) -> dict[str, list[object]]:
    """Map a saved label back to proposal indices (or typed boxes) so it can be edited."""
    choices: dict[str, list[object]] = {}
    for user in label.road_users:
        picks: list[object] = []
        for frame in label.keyframes:
            box = user.boxes.get(frame)
            if box is None:
                picks.append(None)
                continue
            index = next((i for i, candidate in enumerate(proposals[frame]) if [round(v, 3) for v in candidate[:4]] == [round(v, 3) for v in box]), None)
            picks.append(index if index is not None else list(box))
        choices[user.role] = picks
    return choices


def main() -> None:
    args = _arguments()
    st.set_page_config(page_title="Impact labels", layout="wide")
    st.title("Label the colliding road users")
    st.caption("Orange: labelled impact area. Green: numbered detector proposals. Red: your road user A. Blue: your road user B. "
               "The pre-selected answers are suggestions only; check every keyframe.")
    clips = chosen_clips(args.selection, args.split, args.per_type)
    args.labels.mkdir(parents=True, exist_ok=True)
    with st.sidebar:
        annotator = st.text_input("Your name (annotator)", key="annotator").strip()
        done = {clip["local_video_id"] for clip in clips if label_path(args.labels, clip["local_video_id"]).exists()}
        st.write(f"{len(done)} of {len(clips)} clips labelled")
        clip_id = st.selectbox("Clip", [clip["local_video_id"] for clip in clips],
                               format_func=lambda value: f"{'✓' if value in done else '·'} {value} ({next(c['type'] for c in clips if c['local_video_id'] == value)})")
        zoom = st.toggle("Zoom around the impact", value=False)
        width = st.slider("Image width", 600, 1400, 1000, 100)
    clip = next(item for item in clips if item["local_video_id"] == clip_id)
    work = json.loads((args.work / clip_id / "proposals.json").read_text(encoding="utf-8"))
    keyframes = tuple(work["keyframes"])
    proposals = {int(frame): boxes for frame, boxes in work["proposals"].items()}
    video = args.data_root / clip["rgb_path"]
    first = _frame(str(video), keyframes[0])
    height_px, width_px = first.shape[:2]
    fps = float(clip["no_frames"]) / float(clip["duration"])
    impact = int(clip["accident_frame"])
    impact_box = (float(clip["x1"]) * width_px, float(clip["y1"]) * height_px, float(clip["x2"]) * width_px, float(clip["y2"]) * height_px)
    roles = ("A",) if clip["type"] == "single" else ("A", "B")
    cx, cy = (impact_box[0] + impact_box[2]) / 2, (impact_box[1] + impact_box[3]) / 2
    crop = (max(0, round(cx - width_px / 4)), max(0, round(cy - height_px / 4)), min(width_px, round(cx + width_px / 4)), min(height_px, round(cy + height_px / 4)))

    existing = label_path(args.labels, clip_id)
    initial = (_existing_choices(ImpactLabel.model_validate_json(existing.read_text(encoding="utf-8")), proposals) if existing.exists()
               else suggest_choices(keyframes, proposals, impact, impact_box, roles, 3.0))

    st.subheader(f"{clip_id} · {clip['type']} · {clip['day_time']} · {clip['weather']} · labelled impact at frame {impact} ({impact / fps:.2f} s)")
    st.video(str(_preview(video, impact / fps - 3.0, args.work / clip_id / "preview.mp4")))
    st.caption("The preview starts 3 s before the labelled impact.")

    picks: dict[str, list[object]] = {role: [] for role in roles}
    for position, frame in enumerate(keyframes):
        boxes = proposals[frame]
        options = [NOT_VISIBLE] + [f"#{i} {ROAD_USER_CLASSES.get(int(box[4]), '?')} {box[5]:.2f}" for i, box in enumerate(boxes)] + [MANUAL]
        image_column, choice_column = st.columns((4, 1))
        chosen_boxes: dict[str, list[float]] = {}
        with choice_column:
            st.markdown(f"**t {(frame - impact) / fps:+.1f} s · frame {frame}**")
            for role in roles:
                start = initial[role][position]
                default = 0 if start is None else (start + 1 if isinstance(start, int) else len(options) - 1)
                answer = st.selectbox(f"Road user {role}", options, index=default, key=f"{clip_id}-{frame}-{role}")
                if answer == NOT_VISIBLE:
                    picks[role].append(None)
                elif answer == MANUAL:
                    typed = st.text_input(f"{role}: x1, y1, x2, y2 (pixels)", value=", ".join(f"{v:.0f}" for v in start) if isinstance(start, list) else "",
                                          key=f"{clip_id}-{frame}-{role}-box")
                    try:
                        box = [float(value) for value in typed.split(",")]
                        if len(box) != 4 or box[0] >= box[2] or box[1] >= box[3]:
                            raise ValueError
                    except ValueError:
                        st.warning("Enter four numbers: x1, y1, x2, y2 with x1 < x2 and y1 < y2.")
                        box = None
                    picks[role].append(box)
                    if box:
                        chosen_boxes[role] = box
                else:
                    index = int(answer.split()[0][1:])
                    picks[role].append(index)
                    chosen_boxes[role] = boxes[index]
        with image_column:
            image = cv2.cvtColor(_frame(str(video), frame), cv2.COLOR_RGB2BGR)
            panel = draw_panel(image, boxes, impact_box, f"t{(frame - impact) / fps:+.1f}s frame {frame}", crop if zoom else None, width, chosen_boxes)
            st.image(cv2.cvtColor(panel, cv2.COLOR_BGR2RGB))

    notes = st.text_area("Notes (optional)", key=f"{clip_id}-notes")
    if st.button("Save label", type="primary", disabled=not annotator):
        label = label_from_choices(clip, keyframes, proposals, picks, annotator, notes, "verified")
        existing.write_text(label.model_dump_json(indent=2) + "\n", encoding="utf-8")
        st.success(f"Saved {existing}. Pick the next clip in the sidebar.")
    if not annotator:
        st.info("Enter your name in the sidebar to save.")


main()

"""Draft labels of the colliding road users around the impact of real ACCIDENT clips.

`propose` extracts the fixed keyframes of each chosen clip, runs a proposal detector, and writes numbered contact
sheets (full frame and a zoom around the labelled impact box) for a person to inspect. `record` turns that person's
choices (proposal index, explicit box, or null per keyframe) into draft label files.
"""

import argparse
import csv
import json
from pathlib import Path

import cv2
import numpy as np

from minivision_reconstruct.incident.impact_labels import keyframes_for, label_from_choices, label_path

ROAD_USER_CLASSES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
PANEL_WIDTH = 640


def chosen_clips(selection: Path, split: str, per_type: int) -> list[dict[str, str]]:
    """The first clips of each collision type in selection order: fixed before any clip is viewed."""
    with selection.open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.DictReader(stream) if row["split"] == split]
    picked: list[dict[str, str]] = []
    for kind in sorted({row["type"] for row in rows}):
        picked.extend([row for row in rows if row["type"] == kind][:per_type])
    return picked


def read_frame(video: Path, frame: int) -> np.ndarray:
    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
    ok, image = capture.read()
    capture.release()
    if not ok:
        raise SystemExit(f"Could not read frame {frame} of {video}.")
    return image


def _sheet(panels: list[np.ndarray]) -> np.ndarray:
    height = max(panel.shape[0] for panel in panels)
    rows = []
    for start in range(0, len(panels), 3):
        row = [cv2.copyMakeBorder(panel, 0, height - panel.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255)) for panel in panels[start:start + 3]]
        row += [np.full((height, PANEL_WIDTH, 3), 255, np.uint8)] * (3 - len(row))
        rows.append(cv2.hconcat([cv2.copyMakeBorder(cell, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(255, 255, 255)) for cell in row]))
    return cv2.vconcat(rows)


ROLE_COLOURS = {"A": (40, 40, 230), "B": (230, 120, 30)}


def draw_panel(image: np.ndarray, boxes: list[list[float]], impact_box: tuple[float, float, float, float], title: str,
               crop: tuple[int, int, int, int] | None, width: int = PANEL_WIDTH, chosen: dict[str, list[float]] | None = None) -> np.ndarray:
    """One keyframe: the labelled impact box (orange), numbered proposals (green), and any chosen boxes (A red, B blue)."""
    canvas = image.copy()
    x1, y1, x2, y2 = (round(value) for value in impact_box)
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 140, 255), 2)
    for index, box in enumerate(boxes):
        bx1, by1, bx2, by2 = (round(value) for value in box[:4])
        cv2.rectangle(canvas, (bx1, by1), (bx2, by2), (60, 220, 60), 1)
    for role, box in (chosen or {}).items():
        bx1, by1, bx2, by2 = (round(value) for value in box[:4])
        cv2.rectangle(canvas, (bx1, by1), (bx2, by2), ROLE_COLOURS.get(role, (255, 0, 255)), 3)
    if crop:
        canvas = canvas[crop[1]:crop[3], crop[0]:crop[2]]
    scale = width / canvas.shape[1]
    canvas = cv2.resize(canvas, (width, round(canvas.shape[0] * scale)), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    for index, box in enumerate(boxes):
        bx, by = box[0] - (crop[0] if crop else 0), box[1] - (crop[1] if crop else 0)
        px, py = round(bx * scale), round(by * scale)
        if 0 <= px < canvas.shape[1] and 0 <= py < canvas.shape[0]:
            label = str(index)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(canvas, (px, max(py - th - 4, 0)), (px + tw + 4, max(py, th + 4)), (20, 20, 20), -1)
            cv2.putText(canvas, label, (px + 2, max(py - 2, th + 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 22), (20, 20, 20), -1)
    cv2.putText(canvas, title, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return canvas


def propose(args: argparse.Namespace) -> None:
    from ultralytics import YOLO
    model = YOLO(str(args.weights))
    clips = chosen_clips(args.selection, args.split, args.per_type)
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work / "clips.txt").write_text("".join(f"{clip['local_video_id']}\t{clip['type']}\n" for clip in clips), encoding="utf-8")
    for clip in clips:
        video = args.data_root / clip["rgb_path"]
        capture = cv2.VideoCapture(str(video))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        capture.release()
        fps = float(clip["no_frames"]) / float(clip["duration"])
        impact = int(clip["accident_frame"])
        keyframes = keyframes_for(impact, fps, frame_count)
        impact_box = (float(clip["x1"]) * width, float(clip["y1"]) * height, float(clip["x2"]) * width, float(clip["y2"]) * height)
        cx, cy = (impact_box[0] + impact_box[2]) / 2, (impact_box[1] + impact_box[3]) / 2
        half_w, half_h = width * 0.25, height * 0.25
        crop = (max(0, round(cx - half_w)), max(0, round(cy - half_h)), min(width, round(cx + half_w)), min(height, round(cy + half_h)))
        proposals: dict[int, list[list[float]]] = {}
        full, zoom = [], []
        for frame in keyframes:
            image = read_frame(video, frame)
            result = model.predict(image, imgsz=args.imgsz, conf=args.conf, device=args.device, classes=list(ROAD_USER_CLASSES), verbose=False)[0]
            boxes = [[*map(float, box.xyxy[0].tolist()), int(box.cls), float(box.conf)] for box in result.boxes]
            # Number boxes nearest the labelled impact first, so the likely participants get small indices.
            boxes.sort(key=lambda item: ((item[0] + item[2]) / 2 - cx) ** 2 + ((item[1] + item[3]) / 2 - cy) ** 2)
            proposals[frame] = boxes
            title = f"t{(frame - impact) / fps:+.1f}s  frame {frame}"
            full.append(draw_panel(image, boxes, impact_box, title, None))
            zoom.append(draw_panel(image, boxes, impact_box, title, crop))
        clip_dir = args.work / clip["local_video_id"]
        clip_dir.mkdir(exist_ok=True)
        (clip_dir / "proposals.json").write_text(json.dumps({"keyframes": keyframes, "proposals": {str(k): v for k, v in proposals.items()}}), encoding="utf-8")
        cv2.imwrite(str(clip_dir / "full.jpg"), _sheet(full), [cv2.IMWRITE_JPEG_QUALITY, 88])
        cv2.imwrite(str(clip_dir / "zoom.jpg"), _sheet(zoom), [cv2.IMWRITE_JPEG_QUALITY, 88])
        print(f"{clip['local_video_id']}\t{clip['type']}\tkeyframes {len(keyframes)}\tboxes {sum(len(v) for v in proposals.values())}", flush=True)


def record(args: argparse.Namespace) -> None:
    clips = {clip["local_video_id"]: clip for clip in chosen_clips(args.selection, args.split, args.per_type)}
    choices = json.loads(args.choices.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    for clip_id, entry in choices.items():
        work = json.loads((args.work / clip_id / "proposals.json").read_text(encoding="utf-8"))
        proposals = {int(frame): boxes for frame, boxes in work["proposals"].items()}
        label = label_from_choices(clips[clip_id], tuple(work["keyframes"]), proposals, entry["roles"], args.annotator, entry.get("notes", ""))
        label_path(args.output, clip_id).write_text(label.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(choices)} draft labels to {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("propose", "record"):
        command = commands.add_parser(name)
        command.add_argument("--selection", type=Path, required=True)
        command.add_argument("--split", choices=("real_dev", "real_test"), required=True)
        command.add_argument("--per-type", type=int, required=True)
        command.add_argument("--work", type=Path, required=True, help="Git-ignored folder for proposals and contact sheets (real footage).")
        if name == "propose":
            command.add_argument("--data-root", type=Path, required=True)
            command.add_argument("--weights", type=Path, required=True)
            command.add_argument("--imgsz", type=int, required=True)
            command.add_argument("--conf", type=float, required=True)
            command.add_argument("--device", required=True)
        else:
            command.add_argument("--choices", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--annotator", required=True)
    args = parser.parse_args()
    propose(args) if args.command == "propose" else record(args)


if __name__ == "__main__":
    main()

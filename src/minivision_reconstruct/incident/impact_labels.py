"""Labels of the colliding road users around the labelled impact of real ACCIDENT clips, for measuring track continuity.

Keyframes are fixed in advance relative to the benchmark's impact frame: every 0.5 s from 2 s before to 2 s after.
Box proposals come from a stronger detector than the pipeline's; a person chooses which proposal is each colliding
road user at each keyframe, draws a box when none fits, or marks the road user as not visible. Labels stay drafts
until a person has checked them and set the status to "verified".
"""

from pathlib import Path
from typing import Literal

from pydantic import Field

from minivision_reconstruct.incident.models import IncidentError
from minivision_reconstruct.reconstruction.models import Artifact


KEYFRAME_OFFSETS_SECONDS = (-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0)
Box = tuple[float, float, float, float]


class ImpactRoadUser(Artifact):
    role: str = Field(min_length=1, description="A or B for the two colliding road users; A alone for a single-vehicle crash.")
    boxes: dict[int, Box | None] = Field(description="Keyframe -> box in source pixels, or null when the road user is not visible.")


class ImpactLabel(Artifact):
    schema_version: Literal["1.0"]
    clip_id: str
    split: Literal["real_dev", "real_test"]
    collision_type: str
    impact_frame: int = Field(ge=0)
    frame_rate: float = Field(gt=0)
    keyframes: tuple[int, ...] = Field(min_length=1)
    road_users: tuple[ImpactRoadUser, ...] = Field(min_length=1, max_length=2)
    annotator: str = Field(min_length=1)
    status: Literal["draft", "verified"]
    notes: str


def keyframes_for(impact_frame: int, frame_rate: float, frame_count: int) -> tuple[int, ...]:
    """Fixed keyframes around the impact, clipped to the clip; duplicates removed."""
    frames = sorted({min(max(round(impact_frame + offset * frame_rate), 0), frame_count - 1) for offset in KEYFRAME_OFFSETS_SECONDS})
    return tuple(frames)


def label_from_choices(
    clip: dict[str, str], keyframes: tuple[int, ...], proposals: dict[int, list[list[float]]],
    choices: dict[str, list[int | list[float] | None]], annotator: str, notes: str, status: Literal["draft", "verified"] = "draft",
) -> ImpactLabel:
    """Build a label: per role and keyframe, a proposal index, an explicit [x1, y1, x2, y2] box, or None (not visible)."""
    road_users = []
    for role, picks in choices.items():
        if len(picks) != len(keyframes):
            raise IncidentError(f"{clip['local_video_id']}: role {role} has {len(picks)} choices for {len(keyframes)} keyframes.")
        boxes: dict[int, Box | None] = {}
        for frame, pick in zip(keyframes, picks, strict=True):
            if pick is None:
                boxes[frame] = None
            elif isinstance(pick, int):
                if not 0 <= pick < len(proposals[frame]):
                    raise IncidentError(f"{clip['local_video_id']}: no proposal {pick} at frame {frame}.")
                boxes[frame] = tuple(float(value) for value in proposals[frame][pick][:4])  # type: ignore[assignment]
            else:
                boxes[frame] = tuple(float(value) for value in pick)  # type: ignore[assignment]
        road_users.append(ImpactRoadUser(role=role, boxes=boxes))
    fps = float(clip["no_frames"]) / float(clip["duration"])
    return ImpactLabel(schema_version="1.0", clip_id=clip["local_video_id"], split=clip["split"], collision_type=clip["type"],  # type: ignore[arg-type]
                       impact_frame=int(clip["accident_frame"]), frame_rate=fps, keyframes=keyframes, road_users=tuple(road_users),
                       annotator=annotator, status=status, notes=notes)


def suggest_choices(keyframes: tuple[int, ...], proposals: dict[int, list[list[float]]], impact_frame: int, impact_box: Box,
                    roles: tuple[str, ...], max_step: float) -> dict[str, list[int | None]]:
    """A starting point for the person labelling, never a label: at the keyframe nearest the impact, the proposals that
    overlap the labelled impact box most (nearest first on ties); then, moving outward one keyframe at a time, the
    unused proposal nearest the previous choice, if it lies within `max_step` box diagonals."""
    anchor = min(range(len(keyframes)), key=lambda index: abs(keyframes[index] - impact_frame))
    at_impact = proposals[keyframes[anchor]]
    ranked = sorted(range(len(at_impact)), key=lambda index: -_iou(tuple(at_impact[index][:4]), impact_box))  # type: ignore[arg-type]
    choices: dict[str, list[int | None]] = {role: [None] * len(keyframes) for role in roles}
    for role, index in zip(roles, ranked, strict=False):
        choices[role][anchor] = index
    for direction in (-1, 1):
        position = anchor + direction
        while 0 <= position < len(keyframes):
            boxes, used = proposals[keyframes[position]], set()
            for role in roles:
                # The nearest earlier choice on the way back towards the impact keyframe.
                chosen = next(((step, choices[role][step]) for step in range(position - direction, anchor - direction, -direction)
                               if choices[role][step] is not None), None)
                if chosen is None:
                    continue
                reference = proposals[keyframes[chosen[0]]][chosen[1]]  # type: ignore[index]
                diagonal = max(((reference[2] - reference[0]) ** 2 + (reference[3] - reference[1]) ** 2) ** 0.5, 1.0)
                centre = ((reference[0] + reference[2]) / 2, (reference[1] + reference[3]) / 2)
                distances = [(((box[0] + box[2]) / 2 - centre[0]) ** 2 + ((box[1] + box[3]) / 2 - centre[1]) ** 2) ** 0.5 / diagonal
                             for box in boxes]
                candidates = [index for index in sorted(range(len(boxes)), key=distances.__getitem__) if index not in used and distances[index] <= max_step]
                if candidates:
                    choices[role][position] = candidates[0]
                    used.add(candidates[0])
            position += direction
    return choices


def _iou(a: Box, b: Box) -> float:
    width, height = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def track_continuity(label: ImpactLabel, tracks: dict[int, dict[int, Box]], minimum_iou: float) -> dict[str, dict[str, object]]:
    """Per labelled road user: the matching track ID at each visible keyframe, and whether one ID spans the impact.

    A keyframe is covered when some track's box overlaps the label by at least `minimum_iou`. "Same ID through impact"
    means the last covered keyframe before the impact and the first covered keyframe at or after it share a track ID,
    with no visible keyframe uncovered in between.
    """
    result: dict[str, dict[str, object]] = {}
    for user in label.road_users:
        matched: dict[int, int | None] = {}
        for frame, box in user.boxes.items():
            if box is None:
                continue
            best = max(((track_id, _iou(boxes[frame], box)) for track_id, boxes in tracks.items() if frame in boxes), key=lambda item: item[1], default=None)
            matched[frame] = best[0] if best and best[1] >= minimum_iou else None
        before = [frame for frame in sorted(matched) if frame < label.impact_frame]
        after = [frame for frame in sorted(matched) if frame >= label.impact_frame]
        through = bool(before and after and matched[before[-1]] is not None and matched[before[-1]] == matched[after[0]])
        result[user.role] = {"matched_track": matched, "covered": sum(value is not None for value in matched.values()), "visible": len(matched),
                             "same_id_through_impact": through}
    return result


def label_path(root: Path, clip_id: str) -> Path:
    return root / f"{clip_id}.json"

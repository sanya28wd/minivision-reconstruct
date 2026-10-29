"""Pure transformations for finalizing track-state records."""

from dataclasses import replace

from minivision_reconstruct.domain.models import TrackRecord


def interpolate_short_gaps(records: tuple[TrackRecord, ...], maximum_gap_frames: int) -> tuple[TrackRecord, ...]:
    """Replace closed, short lost gaps with linear presentation-only track records."""
    by_track: dict[int, list[TrackRecord]] = {}
    for record in records:
        by_track.setdefault(record.track_id, []).append(record)
    replacements: dict[tuple[int, int], TrackRecord] = {}
    for track_records in by_track.values():
        ordered = sorted(track_records, key=lambda record: record.frame_number)
        for index, first in enumerate(ordered):
            if first.state not in {"observed", "recovered"}:
                continue
            following = ordered[index + 1 :]
            lost = []
            for candidate in following:
                if candidate.state == "lost":
                    lost.append(candidate)
                    continue
                if candidate.state in {"observed", "recovered"}:
                    _replace_gap(first, candidate, tuple(lost), maximum_gap_frames, replacements)
                break
    return tuple(replacements.get((record.track_id, record.frame_number), record) for record in records)


def _replace_gap(
    first: TrackRecord,
    second: TrackRecord,
    lost: tuple[TrackRecord, ...],
    maximum_gap_frames: int,
    replacements: dict[tuple[int, int], TrackRecord],
) -> None:
    if not lost or len(lost) > maximum_gap_frames or first.class_name != second.class_name:
        return
    if not _has_box(first) or not _has_box(second):
        return
    gap = second.frame_number - first.frame_number
    if gap != len(lost) + 1:
        return
    for record in lost:
        fraction = (record.frame_number - first.frame_number) / gap
        replacements[(record.track_id, record.frame_number)] = replace(
            record,
            bbox_x1=_linear(first.bbox_x1, second.bbox_x1, fraction),
            bbox_y1=_linear(first.bbox_y1, second.bbox_y1, fraction),
            bbox_x2=_linear(first.bbox_x2, second.bbox_x2, fraction),
            bbox_y2=_linear(first.bbox_y2, second.bbox_y2, fraction),
            state="interpolated",
        )


def _has_box(record: TrackRecord) -> bool:
    return None not in (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2)


def _linear(first: float | None, second: float | None, fraction: float) -> float:
    if first is None or second is None:
        raise ValueError("Interpolation requires two complete boxes.")
    return first + (second - first) * fraction

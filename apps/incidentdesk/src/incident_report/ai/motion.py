"""Opt-in, local motion proposals. Heuristics are not collision findings."""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import BinaryIO, cast

from incident_report.ai.dense import CandidateWindow, DenseSettings
from incident_report.ai.errors import AIConnectionError

WIDTH = 32  # project assumption, to be validated
HEIGHT = 24  # project assumption, to be validated
MOTION_THRESHOLD = 0.035  # project assumption, to be validated
PIXEL_CHANGE_THRESHOLD = 24  # project assumption, to be validated
CUT_CHANGED_FRACTION = 0.65  # project assumption, to be validated
SHAKE_RESIDUAL_RATIO = 0.35  # project assumption, to be validated
SHIFT_RADIUS = 2  # project assumption, to be validated
PEAK_SEPARATION_SECONDS = 1.0  # project assumption, to be validated
MAX_FRAMES = 30000  # project assumption, to be validated; fail rather than silently omit remaining frames
DECODE_TIMEOUT_SECONDS = 60  # project assumption, to be validated
MAX_WINDOWS = 8  # project assumption, to be validated

# Fixed arguments and inherited seekable input, matching intake's restricted fd protocol.
DECODE_COMMAND = (
    "ffmpeg",
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "info",
    "-threads",
    "1",
    "-protocol_whitelist",
    "fd",
    "-format_whitelist",
    "mov,matroska,webm,avi",
    "-i",
    "fd:",
    "-map",
    "0:v:0",
    "-an",
    "-vf",
    f"scale={WIDTH}:{HEIGHT},format=gray,showinfo",
    "-fps_mode",
    "passthrough",
    "-frames:v",
    str(MAX_FRAMES),
    "-threads",
    "1",
    "-f",
    "rawvideo",
    "pipe:1",
)


@dataclass(frozen=True)
class MotionDetection:
    candidates: tuple[float, ...]
    cuts: tuple[float, ...]
    camera_shake: tuple[float, ...]


def _difference(previous: bytes, current: bytes, dx: int = 0, dy: int = 0) -> float:
    differences = [
        abs(current[y * WIDTH + x] - previous[(y + dy) * WIDTH + x + dx])
        for y in range(SHIFT_RADIUS, HEIGHT - SHIFT_RADIUS)
        for x in range(SHIFT_RADIUS, WIDTH - SHIFT_RADIUS)
    ]
    return sum(differences) / (len(differences) * 255)


@contextmanager
def _temporary_stage() -> Iterator[BinaryIO]:
    with tempfile.TemporaryFile(mode="w+b") as stream:
        yield cast(BinaryIO, stream)


def detect_motion(
    stream: BinaryIO,
    *,
    stage: Callable[[], AbstractContextManager[BinaryIO]] = _temporary_stage,
) -> MotionDetection:
    """Read only media bytes. PTS are relative to the first displayed frame, never nominal FPS.

    Runtime supplies evidence-store staging; standalone timing/tests use temporary files.
    Broad changes (including exposure) and translation-explained changes are kept separately.
    Translation compensation is only a rough shake veto, not calibrated camera geometry.
    """
    stream.seek(0)
    with stage() as frames, stage() as metadata:
        try:
            result = subprocess.run(  # noqa: S603 fixed executable/options, media on inherited stdin
                DECODE_COMMAND,
                stdin=stream,
                stdout=frames,
                stderr=metadata,
                timeout=DECODE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AIConnectionError("Local motion decoding failed or timed out.") from None
        if result.returncode:
            raise AIConnectionError("Local motion decoding failed.")
        metadata.seek(0)
        timestamps = []
        for line in metadata:
            match = re.search(rb"\bn:\s*\d+\s+pts:.*?pts_time:([-+\d.eE]+)", line)
            if match:
                timestamps.append(float(match[1]))
        if not timestamps or len(timestamps) >= MAX_FRAMES or not all(map(math.isfinite, timestamps)):
            raise AIConnectionError("Local motion decoding has missing timestamps or exceeds its frame budget.")
        if any(b <= a for a, b in zip(timestamps, timestamps[1:], strict=False)):
            raise AIConnectionError("Local motion decoding requires increasing presentation timestamps.")
        frames.seek(0)
        previous = frames.read(WIDTH * HEIGHT)
        scores: list[tuple[float, float]] = []
        cuts = []
        shake = []
        for pts in timestamps[1:]:
            current = frames.read(WIDTH * HEIGHT)
            if len(current) != WIDTH * HEIGHT or len(previous) != WIDTH * HEIGHT:
                raise AIConnectionError("Local motion frames and timestamps do not match.")
            timestamp = pts - timestamps[0]
            score = _difference(previous, current)
            changed = sum(abs(a - b) > PIXEL_CHANGE_THRESHOLD for a, b in zip(previous, current, strict=True))
            if score >= MOTION_THRESHOLD:
                residual = min(
                    _difference(previous, current, dx, dy)
                    for dy in range(-SHIFT_RADIUS, SHIFT_RADIUS + 1)
                    for dx in range(-SHIFT_RADIUS, SHIFT_RADIUS + 1)
                )
                if residual <= score * SHAKE_RESIDUAL_RATIO:
                    shake.append(timestamp)
                    score = 0.0
                elif changed / len(current) >= CUT_CHANGED_FRACTION:
                    cuts.append(timestamp)
                    score = 0.0
            scores.append((timestamp, score))
            previous = current
        if frames.read(1):
            raise AIConnectionError("Local motion frames and timestamps do not match.")
    peaks = [
        (timestamp, score)
        for index, (timestamp, score) in enumerate(scores)
        if score >= MOTION_THRESHOLD
        and (index == 0 or score > scores[index - 1][1])
        and (index == len(scores) - 1 or score >= scores[index + 1][1])
    ]
    selected: list[float] = []
    for timestamp, _ in sorted(peaks, key=lambda item: (-item[1], item[0])):
        if all(abs(timestamp - other) >= PEAK_SEPARATION_SECONDS for other in selected):
            selected.append(timestamp)
    return MotionDetection(tuple(sorted(selected)), tuple(cuts), tuple(shake))


def select_windows(
    overview: tuple[CandidateWindow, ...],
    motion: Mapping[str, tuple[float, ...]],
    settings: DenseSettings,
    durations: Mapping[str, float],
    *,
    cap: int = MAX_WINDOWS,
) -> tuple[tuple[CandidateWindow, ...], tuple[CandidateWindow, ...]]:
    """Merge same-view overlaps and always keep overview/both windows.

    The cap limits only motion-only additions to the merged overview union:
    keep at most max(0, cap - overview/both count) motion-only windows.
    Selection and dropped motion windows retain fixed view order, then time order.
    """
    windows = list(overview)
    for view_id, timestamps in motion.items():
        for timestamp in timestamps:
            end = min(timestamp + settings.window_after_seconds, durations.get(view_id, float("inf")))
            start = max(0.0, timestamp - settings.window_before_seconds)
            if start < end and 0 <= timestamp <= end:
                windows.append(
                    CandidateWindow(
                        view_id,
                        timestamp,
                        start,
                        end,
                        "Local motion peak to verify; contact is undetermined.",
                        "motion",
                    )
                )
    merged: list[CandidateWindow] = []
    for window in sorted(windows, key=lambda w: (w.view_id, w.start_seconds, w.end_seconds)):
        if merged and merged[-1].view_id == window.view_id and window.start_seconds <= merged[-1].end_seconds:
            previous = merged.pop()
            merged.append(
                CandidateWindow(
                    previous.view_id,
                    previous.candidate_timestamp,
                    previous.start_seconds,
                    max(previous.end_seconds, window.end_seconds),
                    previous.candidate_description + "\n" + window.candidate_description,
                    previous.source if previous.source == window.source else "both",
                )
            )
        else:
            merged.append(window)
    motion_budget = max(0, cap - sum(window.source in {"overview", "both"} for window in merged))
    selected: list[CandidateWindow] = []
    dropped: list[CandidateWindow] = []
    for window in merged:
        if window.source == "motion":
            if motion_budget == 0:
                dropped.append(window)
                continue
            motion_budget -= 1
        selected.append(window)
    return tuple(selected), tuple(dropped)

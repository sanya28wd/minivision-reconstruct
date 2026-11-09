"""Offline contact proposals, never collision findings; no pipeline wiring or truth input.

FFmpeg-only fine-grid fallback: PyAV's BSD wrapper has GPL-enabled binary wheels.
See docs/research/LOCAL_CRASH_FINDER_A_API.md. Frame-difference energy is a motion
proxy, not optical flow, physical jerk, speed, or directional divergence.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, cast

# Every value below is a project assumption, to be validated (ADR 0006),
# including resolution, temporal sampling, resource budgets and veto thresholds.
WIDTH, HEIGHT, CELL = 160, 96, 8
FPS = 10
MAX_SECONDS = 300
MAX_BYTES = 1_000_000_000
MAX_SOURCE_PIXELS = 4096 * 2160
TIMEOUT = 90
MAX_CANDIDATES = 8
CHANGE_PIXEL = 24
CUT_FRACTION = 0.65
CAMERA_FRACTION = 0.30
CAMERA_RESIDUAL = 0.40
SHIFT_RADIUS = 6
HISTORY = 3
LOCAL_CHANGE = 0.018
RELATIVE_CHANGE = 0.65
MIN_ACTIVE = 0.01
SEPARATION = 1.0
AUDIO_HIGHPASS_HZ = 2000
AUDIO_BLOCK = 1600  # 0.1 s at 16 kHz
AUDIO_NEAR = 0.3
AUDIO_FLOOR_DB = -35.0
AUDIO_BURST_DB = 6.0
AUDIO_SILENCE_DB = -120.0  # numerical floor for -inf bins, not measured energy
COLS, ROWS = WIDTH // CELL, HEIGHT // CELL


@dataclass(frozen=True)
class ContactCandidate:
    timestamp: float  # seconds from first displayed video frame
    area: tuple[int, int, int, int]  # source pixels, x1/y1 inclusive, x2/y2 exclusive
    motion_score: float
    audio_score: float | None  # dB above median high-pass RMS; None = no aligned usable audio


@dataclass(frozen=True)
class ContactCandidates:
    candidates: tuple[ContactCandidate, ...]
    audio_status: Literal["available", "no audio", "unaligned audio"]
    scene_cuts: tuple[float, ...]
    camera_motion: tuple[float, ...]


def _run(command: list[str], output: int | BinaryIO = subprocess.PIPE) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603 fixed options, local validated path, shell disabled
            command, stdout=output, stderr=subprocess.PIPE, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Local media tool unavailable or timed out.") from exc
    if result.returncode:
        raise ValueError("Local media decoding failed.")
    return result.stdout or b""


def _input(path: Path) -> list[str]:
    # No playlist/network demuxers; FFmpeg MOV external data references default off.
    return ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,avi", "-i", str(path)]


def _audio(
    path: Path, start_offset: float
) -> tuple[Literal["available", "no audio", "unaligned audio"], list[tuple[float, float]]]:
    filters = (
        f"asetpts=PTS-STARTPTS,aresample=16000,highpass=f={AUDIO_HIGHPASS_HZ},"
        f"asetnsamples=n={AUDIO_BLOCK}:p=0,astats=metadata=1:reset=1,"
        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    raw = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            *_input(path),
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            str(MAX_SECONDS),
            "-ac",
            "1",
            "-af",
            filters,
            "-f",
            "null",
            "-",
        ]
    ).decode("utf-8", errors="replace")
    values = []
    has_signal = False
    timestamp = 0.0
    for line in raw.splitlines():
        match = re.search(r"pts_time:([-+\d.eE]+)", line)
        if match:
            timestamp = float(match[1]) + start_offset
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            value = float(line.split("=", 1)[1])
            if math.isfinite(value):
                has_signal = True
            values.append((timestamp, max(AUDIO_SILENCE_DB, value)))
    return ("available", values) if has_signal else ("no audio", [])


def _cell_motion(previous: bytes, current: bytes) -> tuple[list[float], float]:
    scores = [0.0] * (COLS * ROWS)
    changed = 0
    for index, (a, b) in enumerate(zip(previous, current, strict=True)):
        difference = abs(a - b)
        scores[(index // WIDTH // CELL) * COLS + (index % WIDTH // CELL)] += difference / (255 * CELL * CELL)
        changed += difference > CHANGE_PIXEL
    return scores, changed / len(current)


def _camera(previous: bytes, current: bytes) -> bool:
    # Sparse global translation fit, only invoked for spatially broad changes.
    def error(dx: int, dy: int) -> float:
        return sum(
            abs(current[y * WIDTH + x] - previous[(y + dy) * WIDTH + x + dx])
            for y in range(SHIFT_RADIUS, HEIGHT - SHIFT_RADIUS, 4)
            for x in range(SHIFT_RADIUS, WIDTH - SHIFT_RADIUS, 4)
        )

    baseline = error(0, 0)
    return (
        baseline > 0
        and min(
            error(dx, dy)
            for dy in range(-SHIFT_RADIUS, SHIFT_RADIUS + 1, 2)
            for dx in range(-SHIFT_RADIUS, SHIFT_RADIUS + 1, 2)
        )
        < baseline * CAMERA_RESIDUAL
    )


def _groups(active: set[int]) -> list[set[int]]:
    groups = []
    while active:
        group = {active.pop()}
        todo = list(group)
        while todo:
            index = todo.pop()
            for adjacent in (index - COLS, index + COLS, index - 1, index + 1):
                if adjacent in active and abs(adjacent % COLS - index % COLS) <= 1:
                    active.remove(adjacent)
                    group.add(adjacent)
                    todo.append(adjacent)
        if len(group) >= 2:  # project assumption: reject isolated-cell noise
            groups.append(group)
    return groups


def find_contact_candidates(video: str | Path) -> ContactCandidates:
    """Analyse one local video, CPU only. Errors refuse partial/oversized inputs.

    Budgets: <=300 s, <=1 GB, <=4096*2160 source pixels; each child <=90 s.
    Raw frames are disk-staged (<=46.1 MB); only two pixel frames are in memory.
    Grid history is bounded by 3001*240 cells. Audio bursts support/rank motion
    candidates; audio never creates a candidate or proves physical contact.
    """
    path = Path(video).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Expected a bounded local video file.")
    metadata = json.loads(
        _run(["ffprobe", "-v", "error", *_input(path), "-show_streams", "-show_format", "-of", "json"])
    )
    videos = [s for s in metadata["streams"] if s["codec_type"] == "video"]
    if not videos:
        raise ValueError("Video stream required.")
    stream = videos[0]
    width, height = int(stream["width"]), int(stream["height"])
    duration = float(metadata["format"].get("duration", "nan"))
    if not (0 < duration <= MAX_SECONDS and 0 < width * height <= MAX_SOURCE_PIXELS):
        raise ValueError("Video exceeds duration/dimension budget or lacks a duration.")
    audios = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    audio_status: Literal["available", "no audio", "unaligned audio"] = "no audio"
    audio: list[tuple[float, float]] = []
    if audios:
        audio_start = float(audios[0].get("start_time", "nan"))
        video_start = float(stream.get("start_time", "nan"))
        if math.isfinite(audio_start) and math.isfinite(video_start):
            audio_status, audio = _audio(path, audio_start - video_start)
        else:
            audio_status = "unaligned audio"
    cuts: list[float] = []
    camera: list[float] = []
    scores: list[list[float]] = []
    veto: list[bool] = []
    with tempfile.TemporaryFile() as frames:
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                "1",
                "-noautorotate",
                *_input(path),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                f"setpts=PTS-STARTPTS,fps={FPS},scale={WIDTH}:{HEIGHT},format=gray",
                "-frames:v",
                str(MAX_SECONDS * FPS + 1),
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            cast(BinaryIO, frames),
        )
        frames.seek(0)
        previous = frames.read(WIDTH * HEIGHT)
        while current := frames.read(WIDTH * HEIGHT):
            if len(current) != WIDTH * HEIGHT:
                raise ValueError("Incomplete decoded frame.")
            cells, fraction = _cell_motion(previous, current)
            timestamp = (len(scores) + 1) / FPS
            cut = fraction >= CUT_FRACTION
            pan = fraction >= CAMERA_FRACTION and _camera(previous, current)
            if cut:
                cuts.append(timestamp)
            if pan:
                camera.append(timestamp)
            veto.append(cut or pan or fraction >= CAMERA_FRACTION)
            scores.append(cells)
            previous = current
        if len(scores) >= MAX_SECONDS * FPS:
            raise ValueError("Decoded frame budget exceeded.")
    proposals = []
    for i in range(HISTORY, len(scores) - HISTORY):
        if any(veto[i - HISTORY : i + HISTORY]):
            continue
        changes = []
        for cell in range(COLS * ROWS):
            x, y = cell % COLS, cell // COLS
            patch = [
                yy * COLS + xx
                for yy in range(max(0, y - 1), min(ROWS, y + 2))
                for xx in range(max(0, x - 1), min(COLS, x + 2))
            ]
            before = sum(scores[t][c] for t in range(i - HISTORY, i) for c in patch) / (HISTORY * len(patch))
            after = sum(scores[t][c] for t in range(i, i + HISTORY) for c in patch) / (HISTORY * len(patch))
            delta = abs(after - before)
            changes.append(
                delta if before >= MIN_ACTIVE and delta >= max(LOCAL_CHANGE, before * RELATIVE_CHANGE) else 0
            )
        for group in _groups({c for c, delta in enumerate(changes) if delta}):
            x1 = max(0, min(c % COLS for c in group) - 1) * CELL
            y1 = max(0, min(c // COLS for c in group) - 1) * CELL
            x2 = min(COLS, max(c % COLS for c in group) + 2) * CELL
            y2 = min(ROWS, max(c // COLS for c in group) + 2) * CELL
            box = (
                x1 * width // WIDTH,
                y1 * height // HEIGHT,
                math.ceil(x2 * width / WIDTH),
                math.ceil(y2 * height / HEIGHT),
            )
            timestamp = (i + 1) / FPS
            baseline = statistics.median(v for _, v in audio) if audio else 0.0
            near = [v for t, v in audio if abs(t - timestamp) <= AUDIO_NEAR]
            audio_score = max(0.0, max(near, default=-math.inf) - baseline) if audio else None
            if audio_score is not None and (not near or max(near) < AUDIO_FLOOR_DB or audio_score < AUDIO_BURST_DB):
                audio_score = 0.0
            proposals.append(ContactCandidate(timestamp, box, max(changes[c] for c in group), audio_score))
    selected: list[ContactCandidate] = []
    for candidate in sorted(proposals, key=lambda c: (-(c.motion_score * (2 if c.audio_score else 1)), c.timestamp)):
        if all(abs(candidate.timestamp - c.timestamp) >= SEPARATION for c in selected):
            selected.append(candidate)
            if len(selected) == MAX_CANDIDATES:
                break
    return ContactCandidates(
        tuple(sorted(selected, key=lambda c: c.timestamp)), audio_status, tuple(cuts), tuple(camera)
    )

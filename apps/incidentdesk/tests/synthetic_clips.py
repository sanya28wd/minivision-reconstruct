"""Deterministic synthetic video fixtures for replay tests.

Clips are tiny MP4s (~3 KB each) generated with bitexact ffmpeg filters.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from incident_report.samples import SAMPLE_VIEW_BYTES

SAMPLE_VIEW_SHA256 = "a13c3dd3e55eeb72b6d635ff322d54f2f481f15a71abbe00419be9c5ff5d617d"

CONTACT_CLIP_SHA256 = "88f7cc85765ef110715ccfb73e125ade4c9175d94d06e5d6f6274e140729b3b1"
NO_CONTACT_CLIP_SHA256 = "2120b0ebc44b3fba42a765b5a2f89db255765f9205a48896aebf794f27f304fc"

_CONTACT_FFMPEG_CMD = [
    "ffmpeg",
    "-y",
    "-f",
    "lavfi",
    "-i",
    "color=c=black:s=64x64:d=2:r=25",
    "-vf",
    "drawbox=x='if(lte(t,1),4+20*t,24)':y=24:w=12:h=12:color=red@1:t=fill,"
    "drawbox=x='if(lte(t,1),48-20*t,28)':y=24:w=12:h=12:color=blue@1:t=fill",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
]

_NO_CONTACT_FFMPEG_CMD = [
    "ffmpeg",
    "-y",
    "-f",
    "lavfi",
    "-i",
    "color=c=black:s=64x64:d=2:r=25",
    "-vf",
    "drawbox=x='4+20*t':y=12:w=12:h=12:color=white@1:t=fill,drawbox=x='48-20*t':y=40:w=12:h=12:color=green@1:t=fill",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
]

_CONTACT_BYTES_CACHE: bytes | None = None
_NO_CONTACT_BYTES_CACHE: bytes | None = None


_REPLAY_CASES_DIR = Path(__file__).parent / "replay_cases"
_CONTACT_FIXTURE_PATH = _REPLAY_CASES_DIR / "case_01_two_party_contact" / "contact_two_boxes.mp4"
_NO_CONTACT_FIXTURE_PATH = _REPLAY_CASES_DIR / "case_02_no_contact" / "no_contact_boxes.mp4"


def _run_ffmpeg_generate(cmd: list[str]) -> bytes | None:
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        full_cmd = cmd + [str(tmp_path)]
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            result = subprocess.run(
                full_cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0 and tmp_path.is_file():
                return tmp_path.read_bytes()
    finally:
        if tmp_path.is_file():
            tmp_path.unlink()
    return None


def get_contact_clip_bytes() -> bytes:
    """Return deterministic contact clip bytes (two boxes moving to collide at center)."""
    global _CONTACT_BYTES_CACHE
    if _CONTACT_BYTES_CACHE is not None:
        return _CONTACT_BYTES_CACHE

    if _CONTACT_FIXTURE_PATH.is_file():
        fixture_bytes = _CONTACT_FIXTURE_PATH.read_bytes()
        if hashlib.sha256(fixture_bytes).hexdigest() == CONTACT_CLIP_SHA256:
            _CONTACT_BYTES_CACHE = fixture_bytes
            return _CONTACT_BYTES_CACHE

    raw = _run_ffmpeg_generate(_CONTACT_FFMPEG_CMD)
    if raw is None or hashlib.sha256(raw).hexdigest() != CONTACT_CLIP_SHA256:
        raise RuntimeError("ffmpeg failed to generate deterministic contact clip.")

    _CONTACT_BYTES_CACHE = raw
    return _CONTACT_BYTES_CACHE


def get_no_contact_clip_bytes() -> bytes:
    """Return deterministic no-contact clip bytes (two boxes passing in parallel lanes)."""
    global _NO_CONTACT_BYTES_CACHE
    if _NO_CONTACT_BYTES_CACHE is not None:
        return _NO_CONTACT_BYTES_CACHE

    if _NO_CONTACT_FIXTURE_PATH.is_file():
        fixture_bytes = _NO_CONTACT_FIXTURE_PATH.read_bytes()
        if hashlib.sha256(fixture_bytes).hexdigest() == NO_CONTACT_CLIP_SHA256:
            _NO_CONTACT_BYTES_CACHE = fixture_bytes
            return _NO_CONTACT_BYTES_CACHE

    raw = _run_ffmpeg_generate(_NO_CONTACT_FFMPEG_CMD)
    if raw is None or hashlib.sha256(raw).hexdigest() != NO_CONTACT_CLIP_SHA256:
        raise RuntimeError("ffmpeg failed to generate deterministic no-contact clip.")

    _NO_CONTACT_BYTES_CACHE = raw
    return _NO_CONTACT_BYTES_CACHE


def get_synthetic_clip_by_sha256(digest: str) -> bytes | None:
    """Resolve a clip by SHA-256 if it is one of the known synthetic clips."""
    if digest == CONTACT_CLIP_SHA256:
        return get_contact_clip_bytes()
    if digest == NO_CONTACT_CLIP_SHA256:
        return get_no_contact_clip_bytes()
    if digest == SAMPLE_VIEW_SHA256:
        return SAMPLE_VIEW_BYTES
    return None

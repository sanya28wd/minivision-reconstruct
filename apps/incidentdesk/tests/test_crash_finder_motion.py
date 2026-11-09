import subprocess
from pathlib import Path

import pytest

from incident_report.ai.crash_finder_motion import find_contact_candidates


def make_clip(tmp_path: Path, graph: str, *, audio: bool = False) -> Path:
    path = tmp_path / "synthetic.mp4"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", graph]
    if audio:
        command += [
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=if(between(t\\,1.9\\,2.1)\\,0.8*sin(2*PI*4000*t)\\,0):s=16000:d=4",
            "-c:a",
            "aac",
        ]
    command += ["-t", "4", "-c:v", "mpeg4", "-q:v", "2", "-y", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    return path


STOP = "color=black:s=320x192:r=20:d=4[bg];color=white:s=32x32:r=20:d=4[b];[bg][b]overlay=x=40+50*min(t\\,2):y=80:eval=frame"


def test_local_abrupt_stop_has_time_and_area(tmp_path: Path) -> None:
    result = find_contact_candidates(make_clip(tmp_path, STOP))
    near = [c for c in result.candidates if abs(c.timestamp - 2.0) <= 0.35]
    assert near, result
    assert any(c.area[0] <= 156 <= c.area[2] and c.area[1] <= 96 <= c.area[3] for c in near)
    assert all(c.motion_score > 0 for c in near)
    assert result.audio_status == "no audio"
    assert all(c.audio_score is None for c in result.candidates)


def test_high_frequency_burst_supports_motion_candidate(tmp_path: Path) -> None:
    result = find_contact_candidates(make_clip(tmp_path, STOP, audio=True))
    assert result.audio_status == "available"
    assert any(
        c.audio_score is not None and c.audio_score > 0 and abs(c.timestamp - 2) < 0.35 for c in result.candidates
    )


def test_uniform_camera_pan_has_no_candidates(tmp_path: Path) -> None:
    graph = "nullsrc=s=640x192:r=20:d=4,geq=lum=mod(X*17+Y*23\\,256):cb=128:cr=128,crop=320:192:x=20*t:y=0"
    result = find_contact_candidates(make_clip(tmp_path, graph))
    assert result.candidates == ()
    assert result.camera_motion


def test_hard_cut_has_no_candidates(tmp_path: Path) -> None:
    graph = "color=black:s=320x192:r=20:d=2[a];color=white:s=320x192:r=20:d=2[b];[a][b]concat=n=2:v=1:a=0"
    result = find_contact_candidates(make_clip(tmp_path, graph))
    assert result.candidates == ()
    assert any(abs(t - 2.0) < 0.2 for t in result.scene_cuts)


def test_silent_audio_stream_is_no_audio(tmp_path: Path) -> None:
    video = make_clip(tmp_path, STOP)
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(video),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "4",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(silent),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = find_contact_candidates(silent)
    assert result.candidates
    assert result.audio_status == "no audio"
    assert all(c.audio_score is None for c in result.candidates)


@pytest.mark.parametrize("media_variant", ["ordinary", "delayed", "multiple_streams"])
def test_measurement_scores_first_touch_and_writes_crops(tmp_path: Path, media_variant: str) -> None:
    import json
    import shutil
    import sys

    video = make_clip(tmp_path, STOP)
    if media_variant == "delayed":
        delayed = tmp_path / "delayed.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-itsoffset",
                "1",
                "-i",
                str(video),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-t",
                "5",
                str(delayed),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        video = delayed
    if media_variant == "multiple_streams":
        multiple = tmp_path / "multiple.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(video),
                "-f",
                "lavfi",
                "-i",
                "color=black:s=640x384:r=20:d=4",
                "-map",
                "0:v",
                "-map",
                "1:v",
                "-c:v",
                "mpeg4",
                "-disposition:v:0",
                "0",
                "-disposition:v:1",
                "default",
                "-t",
                "4",
                str(multiple),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        video = multiple
    archive = tmp_path / "dataset/archive/videos"
    truth = tmp_path / "dataset/truth/gemini_dense"
    archive.mkdir(parents=True)
    truth.mkdir(parents=True)
    clips = ("MODERATE_04", "MODERATE_05", "MINOR_01", "VIDEO1", "video3", "night1", "MAJOR_06", "day1")
    for clip in clips:
        shutil.copyfile(video, archive / f"{clip}.mp4")
        collisions = [] if clip in ("night1", "day1") else [{"timestamp": "00:02.000"}]
        (truth / f"{clip}.json").write_text(json.dumps({"truth": {"collisions": collisions}}), encoding="utf-8")
    output = tmp_path / "measurement"
    measured = subprocess.run(
        [
            sys.executable,
            "scripts/measure_crash_finder.py",
            "--method",
            "a",
            "--root",
            str(tmp_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert measured.returncode == 0, measured.stdout + measured.stderr
    rows = json.loads((output / "measurement.json").read_text(encoding="utf-8"))
    assert len(rows) == 8
    assert rows[0]["hit"] is True
    assert rows[0]["truth_contact_seconds"] == [2.0]
    assert all(Path(p).is_file() for p in rows[0]["cropped_stills"])
    nearest = min(enumerate(rows[0]["candidates"]), key=lambda item: abs(item[1]["timestamp"] - 2))
    index, candidate = nearest
    pixels = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            rows[0]["cropped_stills"][index],
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    x1, y1, x2, _ = candidate["area"]
    assert pixels[(96 - y1) * (x2 - x1) + (156 - x1)] > 200
    assert rows[5]["hit"] is None
    assert rows[5]["false_candidates"]


def test_lossless_silence_does_not_hide_audio_burst(tmp_path: Path) -> None:
    video = make_clip(tmp_path, STOP)
    path = tmp_path / "pcm.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(video),
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=if(between(t\\,1.9\\,2.1)\\,0.8*sin(2*PI*4000*t)\\,0):s=16000:d=4",
            "-c:v",
            "copy",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = find_contact_candidates(path)
    assert result.audio_status == "available"
    assert any(
        c.audio_score is not None and c.audio_score > 0 and abs(c.timestamp - 2) < 0.35 for c in result.candidates
    )


def test_missing_time_origin_keeps_audio_support_unknown(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
    import json
    from typing import Any

    video = make_clip(tmp_path, STOP, audio=True)
    original_run = subprocess.run

    def probe_without_origin(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        completed = original_run(command, **kwargs)
        if command[0] == "ffprobe":
            data = json.loads(completed.stdout)
            for stream in data["streams"]:
                stream.pop("start_time", None)
            completed.stdout = json.dumps(data).encode()
        return completed

    monkeypatch.setattr(subprocess, "run", probe_without_origin)
    result = find_contact_candidates(video)
    assert result.candidates
    assert result.audio_status == "unaligned audio"
    assert all(c.audio_score is None for c in result.candidates)


def test_duration_budget_refuses_partial_analysis(tmp_path: Path) -> None:
    path = tmp_path / "long.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=16x16:r=1:d=301",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with pytest.raises(ValueError, match="budget"):
        find_contact_candidates(path)


def test_invalid_media_is_an_error_not_no_contact(tmp_path: Path) -> None:
    path = tmp_path / "invalid.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(ValueError, match="decoding failed"):
        find_contact_candidates(path)

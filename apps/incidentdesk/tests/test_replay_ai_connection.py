"""Seam 2, recorded/replay adapter: contract tests through AIConnection.analyse."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_recording
from incident_report.ai import (
    AIConnectionError,
    AIRequest,
    AIViewRef,
    RecordingInvalid,
    RecordingNotFound,
    ReplayAIConnection,
)
from incident_report.domain import Role

HASH = "ab" * 32


def _request(input_hash: str = HASH, prompt_version: str = "v1") -> AIRequest:
    return AIRequest(input_hash, prompt_version, (AIViewRef(Role.EXTERIOR, "cd" * 32),))


def test_returns_the_recorded_summary_and_its_provenance(recordings_dir: Path) -> None:
    write_recording(recordings_dir, HASH, "v1", "Recorded.")

    response = ReplayAIConnection(recordings_dir).analyse(_request())

    assert (response.summary, response.provenance) == ("Recorded.", "synthetic")


def test_a_missing_recording_is_a_plain_error_not_an_answer(recordings_dir: Path) -> None:
    with pytest.raises(RecordingNotFound, match="No recorded AI response"):
        ReplayAIConnection(recordings_dir).analyse(_request())


@pytest.mark.parametrize("prompt_version", ["../escape", "a/b", "", "v1\x00", "x" * 65, ".hidden"])
def test_a_prompt_version_cannot_name_a_path(recordings_dir: Path, prompt_version: str) -> None:
    with pytest.raises(AIConnectionError):
        ReplayAIConnection(recordings_dir).analyse(_request(prompt_version=prompt_version))


@pytest.mark.parametrize("input_hash", ["../../etc/passwd", "AB" * 32, "ab" * 31, ""])
def test_an_input_hash_must_be_a_sha256_hex_digest(recordings_dir: Path, input_hash: str) -> None:
    with pytest.raises(AIConnectionError):
        ReplayAIConnection(recordings_dir).analyse(_request(input_hash=input_hash))


def test_a_recording_filed_under_the_wrong_key_is_rejected(recordings_dir: Path) -> None:
    other = "cd" * 32
    write_recording(recordings_dir, other, "v1", "Belongs to another input.")
    (recordings_dir / f"{other}.v1.json").rename(recordings_dir / f"{HASH}.v1.json")

    with pytest.raises(RecordingInvalid, match="does not match its own key"):
        ReplayAIConnection(recordings_dir).analyse(_request())


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"input_hash": HASH, "prompt_version": "v1"}),
        json.dumps(
            {"provenance": "synthetic", "input_hash": HASH, "prompt_version": "v1", "response": {"summary": ""}}
        ),
        json.dumps({"provenance": "synthetic", "input_hash": HASH, "prompt_version": "v1", "response": {"summary": 5}}),
    ],
)
def test_an_unreadable_or_incomplete_recording_is_a_plain_error(recordings_dir: Path, content: str) -> None:
    (recordings_dir / f"{HASH}.v1.json").write_text(content, encoding="utf-8")

    with pytest.raises(RecordingInvalid):
        ReplayAIConnection(recordings_dir).analyse(_request())


def test_repair_loads_standalone_repair_file(recordings_dir: Path) -> None:
    write_recording(recordings_dir, HASH, "v1", "Initial summary.")
    repair_file = recordings_dir / f"{HASH}.v1-repair.json"
    repair_file.write_text(
        json.dumps(
            {
                "input_hash": HASH,
                "prompt_version": "v1-repair",
                "provenance": "synthetic-fixture",
                "response": {
                    "summary": "Repaired summary.",
                    "facts": {"parties": [], "events": [], "candidate_collisions": []},
                },
            }
        ),
        encoding="utf-8",
    )

    req = _request()
    ai = ReplayAIConnection(recordings_dir)
    initial_resp = ai.analyse(req)
    repaired_resp = ai.repair(req, initial_resp, ["error timestamp"])

    assert repaired_resp.summary == "Repaired summary."
    assert repaired_resp.provenance == "synthetic-fixture"
    assert repaired_resp.facts is not None


def test_repair_without_repair_file_returns_fallback_safe(recordings_dir: Path) -> None:
    write_recording(recordings_dir, HASH, "v1", "Initial summary.")
    req = _request()
    ai = ReplayAIConnection(recordings_dir)
    initial_resp = ai.analyse(req)
    repaired_resp = ai.repair(req, initial_resp, ["error timestamp"])

    assert repaired_resp.summary == "Repair not recorded."
    assert repaired_resp.facts is None

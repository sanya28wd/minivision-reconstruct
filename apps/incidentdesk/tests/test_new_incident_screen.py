"""Tests for the new-incident screen (RPT-20, spec 001, ADR 0003, ADR 0006).

Acceptance criteria:
- Multiple files per box accepted; each becomes a view with role, label, acquisition method, fingerprint.
- Officer text is stored but absent from all analysis requests (test inspects recorded requests).
- Each view's facts are produced independently before any comparison.
- Keyboard-accessible upload with progress and clear errors.
- Traceability (ADR 0006): no unsourced figure, threshold or category introduced.
- Tests for this ticket pass; full fast test suite passes.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from access_client import authorized_client
from conftest import make_view, write_recording
from incident_report.ai import AIConnectionError, AIRequest, AIResponse, ReplayAIConnection
from incident_report.domain import Origin, Role, RunStatus
from incident_report.entry import PROMPT_VERSION, analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.intake_limits import IntakeLimits
from incident_report.model_calls import ModelCall
from incident_report.runlog import RunLog
from incident_report.storage import LocalEvidenceStore
from incident_report.web import create_app
from incident_report.web.uploads import InvalidUploadForm, UploadTooLarge, staged_form
from video_fixtures import synthetic_video


class SimplePage(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text_parts: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.text_parts).split())

    def count(self, tag: str) -> int:
        return sum(1 for t, _ in self.tags if t == tag)

    def inputs_by_id(self) -> dict[str, dict[str, str | None]]:
        result: dict[str, dict[str, str | None]] = {}
        for t, a in self.tags:
            if t in ("input", "textarea") and (val := a.get("id")) is not None:
                result[val] = a
        return result


class SpyAIConnection:
    """Spy AI connection capturing all AIRequests, Window analyses, and ModelCalls."""

    def __init__(
        self,
        *,
        per_view_facts: dict[str, OverviewFacts] | None = None,
        default_facts: OverviewFacts | None = None,
        fail_views: dict[str, AIConnectionError] | None = None,
        repair_facts: dict[str, OverviewFacts] | None = None,
    ) -> None:
        self.per_view_facts = per_view_facts or {}
        self.default_facts = default_facts
        self.fail_views = fail_views or {}
        self.repair_facts = repair_facts or {}
        self.received_requests: list[AIRequest] = []
        self.received_windows: list[tuple[AIRequest, Any]] = []
        self.received_repairs: list[tuple[AIRequest, AIResponse, tuple[str, ...]]] = []
        self.calls_produced: list[ModelCall] = []

    def analyse(self, request: AIRequest) -> AIResponse:
        self.received_requests.append(request)
        view_key = request.views[0].sha256 if len(request.views) == 1 else "joint"
        if view_key in self.fail_views:
            raise self.fail_views[view_key]
        facts = self.per_view_facts.get(view_key, self.default_facts)
        call = ModelCall(
            request_hash=request.input_hash,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            model="spy-model",
            view_id=view_key,
            fps=1.0,
            raw_response=f"spy-response-for-{view_key[:8]}",
            latency_ms=5.0,
        )
        self.calls_produced.append(call)
        return AIResponse(
            summary=f"Analysis of {view_key[:8]}",
            provenance="spy",
            facts=facts,
            calls=(call,),
        )

    def analyse_window(self, request: AIRequest, window: Any) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
        self.received_windows.append((request, window))
        call = ModelCall(
            request_hash=request.input_hash,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            model="spy-dense-model",
            view_id="dense-window",
            fps=1.0,
            raw_response="spy-dense-response",
            latency_ms=10.0,
        )
        self.calls_produced.append(call)
        return OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
        ), (call,)

    def repair(self, request: AIRequest, response: AIResponse, errors: tuple[str, ...]) -> AIResponse:
        self.received_repairs.append((request, response, errors))
        view_key = request.views[0].sha256 if len(request.views) == 1 else "joint"
        call = ModelCall(
            request_hash=request.input_hash,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            model="spy-repair-model",
            view_id=view_key,
            fps=1.0,
            raw_response="spy-repair-response",
            latency_ms=10.0,
        )
        self.calls_produced.append(call)
        repaired = self.repair_facts.get(view_key, self.default_facts)
        return AIResponse(
            summary="Repaired facts",
            provenance="spy-repair",
            facts=repaired,
            calls=(call,),
        )


async def make_multipart_boundary_stream(
    fields: list[tuple[str, str]], files: list[tuple[str, str, bytes]]
) -> tuple[list[tuple[bytes, bytes]], list[bytes]]:
    boundary = "boundary1234567890"
    body = bytearray()
    for name, val in fields:
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    for field_name, filename, data in files:
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n".encode()
        )
        body.extend(data)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    headers = [
        (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    chunks = [bytes(body[i : i + 256]) for i in range(0, len(body), 256)]
    return headers, chunks


@pytest.fixture(autouse=True)
def gemini_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")


@pytest.fixture
def client(recordings_dir: Path, run_log: RunLog, tmp_path: Path) -> TestClient:
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    return authorized_client(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=evidence),
        run_log.database_url,
    )


def test_new_incident_screen_presents_interior_and_exterior_boxes_and_question_box(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200

    parsed = SimplePage(page.text)
    # Accessible controls: real form, file inputs with multiple attribute
    inputs = parsed.inputs_by_id()

    # Interior drop box inputs
    assert "interior_files" in inputs
    assert inputs["interior_files"].get("type") == "file"
    assert "multiple" in inputs["interior_files"]
    assert "interior_label" in inputs
    assert "interior_acquisition_method" in inputs

    # Exterior drop box inputs
    assert "exterior_files" in inputs
    assert inputs["exterior_files"].get("type") == "file"
    assert "multiple" in inputs["exterior_files"]
    assert "exterior_label" in inputs
    assert "exterior_acquisition_method" in inputs

    # Officer question box
    assert "officer_question" in inputs

    # Each input has a matching <label for="...">
    labelled_for = {a["for"] for t, a in parsed.tags if t == "label" and a.get("for")}
    for field_id in (
        "interior_files",
        "interior_label",
        "interior_acquisition_method",
        "exterior_files",
        "exterior_label",
        "exterior_acquisition_method",
        "officer_question",
    ):
        assert field_id in labelled_for, f"Missing label for {field_id}"

    # Owner ruling 2026-10-25: one deferred same-origin enhancement, with no body.
    assert parsed.count("script") == 1
    (script,) = [attrs for tag, attrs in parsed.tags if tag == "script"]
    assert script == {"src": "/static/early-upload.js", "defer": None}
    assert re.findall(r"<script\b[^>]*>(.*?)</script\s*>", page.text, flags=re.I | re.S) == [""]
    assert parsed.count("style") == 0
    for _tag, attrs in parsed.tags:
        assert "style" not in attrs
        assert not [k for k in attrs if k.startswith("on")]

    # Single submit button
    assert parsed.count("button") == 1
    btn_attrs = next(a for t, a in parsed.tags if t == "button")
    assert btn_attrs.get("type") == "submit"


def test_multiple_files_per_box_accepted_and_fingerprinted(
    client: TestClient, recordings_dir: Path, run_log: RunLog
) -> None:
    # 2 interior clips and 2 exterior clips
    int1 = synthetic_video(b"int-cam-1")
    int2 = synthetic_video(b"int-cam-2")
    ext1 = synthetic_video(b"ext-dash-1")
    ext2 = synthetic_video(b"ext-cctv-2")

    hash_int1 = sha256_hex(int1)
    hash_int2 = sha256_hex(int2)
    hash_ext1 = sha256_hex(ext1)
    hash_ext2 = sha256_hex(ext2)

    # Combined input hash for the 4 views
    input_hash = incident_input_hash(
        [
            (Role.INTERIOR, hash_int1),
            (Role.INTERIOR, hash_int2),
            (Role.EXTERIOR, hash_ext1),
            (Role.EXTERIOR, hash_ext2),
        ]
    )
    write_recording(recordings_dir, input_hash, PROMPT_VERSION, "Multi-view incident report summary.")

    files = [
        ("interior_files", ("cabin_driver.mp4", int1, "video/mp4")),
        ("interior_files", ("cabin_passenger.mp4", int2, "video/mp4")),
        ("exterior_files", ("dashcam.mp4", ext1, "video/mp4")),
        ("exterior_files", ("cctv.mp4", ext2, "video/mp4")),
    ]
    data = {
        "interior_label": "cabin cameras",
        "interior_acquisition_method": "DVR export",
        "exterior_label": "road views",
        "exterior_acquisition_method": "Traffic police secure export",
    }

    response = client.post("/reports", data=data, files=files)
    assert response.status_code == 200  # redirected and rendered

    (run,) = run_log.list_runs()
    assert run.status is RunStatus.SUCCEEDED
    assert run.report is not None
    assert len(run.report.views) == 4

    roles_and_hashes = {(v.role, v.sha256, v.filename) for v in run.report.views}
    assert (Role.INTERIOR, hash_int1, "cabin_driver.mp4") in roles_and_hashes
    assert (Role.INTERIOR, hash_int2, "cabin_passenger.mp4") in roles_and_hashes
    assert (Role.EXTERIOR, hash_ext1, "dashcam.mp4") in roles_and_hashes
    assert (Role.EXTERIOR, hash_ext2, "cctv.mp4") in roles_and_hashes

    # Durably stored in evidence_intake
    intake = run_log.intake_views(run.run_id)
    assert len(intake) == 4
    intake_hashes = {v.sha256 for v in intake}
    assert intake_hashes == {hash_int1, hash_int2, hash_ext1, hash_ext2}

    # Header in rendered report contains all fingerprints
    for h in (hash_int1, hash_int2, hash_ext1, hash_ext2):
        assert h in response.text


def test_officer_question_stored_separately_and_never_sent_to_analysis(
    run_log: RunLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof that officer question text is isolated from single-view, multi-view and dense AIRequests."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    clip1 = synthetic_video(b"clip-question-1")
    clip2 = synthetic_video(b"clip-question-2")
    hash1 = sha256_hex(clip1)
    hash2 = sha256_hex(clip2)
    question_text = "CONFIDENTIAL_OFFICER_QUESTION_TOKEN_888"

    facts1 = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[PartyFact(description="white sedan", view_id=hash1, timestamp_seconds=0.2, confidence=0.9)],
        events=[CitedFact(description="brakes applied", view_id=hash1, timestamp_seconds=0.4, confidence=0.85)],
        candidate_collisions=[
            CitedFact(description="impact on bumper", view_id=hash1, timestamp_seconds=0.5, confidence=0.9)
        ],
    )
    facts2 = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[PartyFact(description="black taxi", view_id=hash2, timestamp_seconds=0.3, confidence=0.9)],
        events=[CitedFact(description="enters lane", view_id=hash2, timestamp_seconds=0.4, confidence=0.85)],
        candidate_collisions=[],
    )

    spy_ai = SpyAIConnection(per_view_facts={hash1: facts1, hash2: facts2}, default_facts=facts1)
    store = LocalEvidenceStore(tmp_path / "isolation_store")

    # 1. Single-view path via HTTP route
    app = create_app(ai=spy_ai, run_log=run_log, evidence=store)
    client = authorized_client(app, run_log.database_url)

    files = [("exterior_files", ("front.mp4", clip1, "video/mp4"))]
    data = {
        "exterior_label": "front dashcam",
        "exterior_acquisition_method": "driver SD card",
        "officer_question": question_text,
    }

    response = client.post("/reports", data=data, files=files)
    assert response.status_code == 200

    runs = run_log.list_runs()
    assert len(runs) == 1
    run1 = runs[0]
    assert run1.status is RunStatus.SUCCEEDED

    # 1a. Stored separately in database
    saved_question = run_log.get_officer_question(run1.run_id)
    assert saved_question == question_text

    # 1b. Proved absent from every recorded AIRequest received by the model connection
    assert len(spy_ai.received_requests) >= 1
    for req in spy_ai.received_requests:
        assert question_text not in repr(req)
        assert question_text not in req.input_hash
        assert question_text not in req.prompt_version
        for v in req.views:
            assert question_text not in v.sha256
            assert question_text not in str(v.role)

    # 1c. Proved absent from all ModelCalls
    for call in spy_ai.calls_produced:
        assert question_text not in repr(call)
        assert question_text not in (call.raw_response or "")
        assert question_text not in (call.request_hash or "")

    # 1d. Proved absent from report fields and rendered HTML page
    assert run1.report is not None
    assert question_text not in repr(run1.report.facts)
    assert question_text not in run1.report.summary
    assert question_text not in run1.report.provenance
    assert question_text not in response.text

    # 2. Multi-view independent path and 3. Dense collision pass via analyse_incident
    spy_ai.received_requests.clear()
    spy_ai.received_windows.clear()
    spy_ai.calls_produced.clear()

    views = [
        make_view(clip1, Role.EXTERIOR, label="dashcam"),
        make_view(clip2, Role.INTERIOR, label="cabin"),
    ]
    report2 = analyse_incident(
        views,
        Origin.APP,
        ai=spy_ai,
        run_log=run_log,
        prompt_version="overview-1",
        dense_prompt_version="dense-1",
        evidence=store,
        officer_question=question_text,
    )

    # Multi-view: exactly 2 per-view independent AIRequests
    assert len(spy_ai.received_requests) == 2
    for req in spy_ai.received_requests:
        assert question_text not in repr(req)
        assert question_text not in req.input_hash
        assert question_text not in req.prompt_version

    # Dense pass was triggered on candidate collision window
    assert len(spy_ai.received_windows) >= 1
    for d_req, win in spy_ai.received_windows:
        assert question_text not in repr(d_req)
        assert question_text not in repr(win)

    # All generated ModelCalls are clean of officer question text
    for call in spy_ai.calls_produced:
        assert question_text not in repr(call)
        assert question_text not in (call.raw_response or "")

    assert report2.facts is not None
    assert question_text not in repr(report2.facts)


def test_each_view_is_analysed_independently_before_comparison(run_log: RunLog, tmp_path: Path) -> None:
    int_vid = synthetic_video(b"interior-independent")
    ext_vid = synthetic_video(b"exterior-independent")
    int_hash = sha256_hex(int_vid)
    ext_hash = sha256_hex(ext_vid)

    int_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description="driver in blue shirt", view_id=int_hash, timestamp_seconds=0.2, confidence=0.9)
        ],
        events=[
            CitedFact(
                description="driver checks rearview mirror", view_id=int_hash, timestamp_seconds=0.5, confidence=0.85
            )
        ],
        candidate_collisions=[],
    )
    ext_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description="white Toyota Camry sedan", view_id=ext_hash, timestamp_seconds=0.3, confidence=0.95)
        ],
        events=[
            CitedFact(
                description="white Toyota Camry brakes suddenly",
                view_id=ext_hash,
                timestamp_seconds=0.6,
                confidence=0.9,
            )
        ],
        candidate_collisions=[
            CitedFact(description="contact with curb", view_id=ext_hash, timestamp_seconds=0.8, confidence=0.88)
        ],
    )

    ai = SpyAIConnection(per_view_facts={int_hash: int_facts, ext_hash: ext_facts})

    views = [
        make_view(int_vid, Role.INTERIOR, label="driver cam"),
        make_view(ext_vid, Role.EXTERIOR, label="front dashcam"),
    ]

    report = analyse_incident(
        views,
        Origin.APP,
        ai=ai,
        run_log=run_log,
        prompt_version="overview-1",
        evidence=LocalEvidenceStore(tmp_path / "independent"),
    )

    # Verify ADR 0003: exactly 2 independent calls, one view each
    assert len(ai.received_requests) == 2
    for req in ai.received_requests:
        assert len(req.views) == 1
    requested_view_hashes = {req.views[0].sha256 for req in ai.received_requests}
    assert requested_view_hashes == {int_hash, ext_hash}

    # Combined facts contains citations from both views, produced independently
    assert report.facts is not None
    assert len(report.facts.parties) == 2
    party_views = {p.view_id for p in report.facts.parties}
    assert party_views == {int_hash, ext_hash}
    assert report.is_valid is True


def test_multiview_preserves_earlier_view_calls_when_later_view_fails(run_log: RunLog, tmp_path: Path) -> None:
    """When a subsequent view raises AIConnectionError, earlier views' calls are preserved in the Run log."""
    v1_vid = synthetic_video(b"view-1-succeeds")
    v2_vid = synthetic_video(b"view-2-fails")
    v1_hash = sha256_hex(v1_vid)
    v2_hash = sha256_hex(v2_vid)

    v1_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[PartyFact(description="silver sedan", view_id=v1_hash, timestamp_seconds=0.1, confidence=0.9)],
        events=[],
        candidate_collisions=[],
    )

    call_failed = ModelCall(
        request_hash="req-failed-hash",
        input_hash="input-failed-hash",
        prompt_version="overview-1",
        model="spy-model",
        view_id=v2_hash,
        fps=1.0,
        raw_response="simulated crash mid-stream",
        latency_ms=15.0,
    )
    fail_exc = AIConnectionError("View 2 failed to analyse", calls=(call_failed,))

    ai = SpyAIConnection(
        per_view_facts={v1_hash: v1_facts},
        fail_views={v2_hash: fail_exc},
    )

    views = [
        make_view(v1_vid, Role.EXTERIOR, label="front"),
        make_view(v2_vid, Role.INTERIOR, label="cabin"),
    ]

    with pytest.raises(AIConnectionError, match="View 2 failed"):
        analyse_incident(
            views,
            Origin.APP,
            ai=ai,
            run_log=run_log,
            prompt_version="overview-1",
            evidence=LocalEvidenceStore(tmp_path / "mid_fail_store"),
        )

    # Verify that Run log captured BOTH View 1's successful call and View 2's failed call
    runs = run_log.list_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run.status is RunStatus.FAILED
    assert len(run.calls) == 2
    assert run.calls[0].view_id == v1_hash
    assert run.calls[1].view_id == v2_hash


def test_per_view_citation_rejects_cross_view_hash(run_log: RunLog, tmp_path: Path) -> None:
    """During per-view analysis, a fact citing another view's hash fails per-view validation."""
    v1_vid = synthetic_video(b"view-1-cross")
    v2_vid = synthetic_video(b"view-2-cross")
    v1_hash = sha256_hex(v1_vid)
    v2_hash = sha256_hex(v2_vid)

    # View 1 tries to cite View 2's hash (cross-view leakage before comparison)
    v1_bad_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[],
        events=[CitedFact(description="saw other car", view_id=v2_hash, timestamp_seconds=0.2, confidence=0.9)],
        candidate_collisions=[],
    )
    v2_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[],
        events=[CitedFact(description="proper event", view_id=v2_hash, timestamp_seconds=0.2, confidence=0.9)],
        candidate_collisions=[],
    )

    ai = SpyAIConnection(per_view_facts={v1_hash: v1_bad_facts, v2_hash: v2_facts})

    views = [
        make_view(v1_vid, Role.INTERIOR),
        make_view(v2_vid, Role.EXTERIOR),
    ]

    report = analyse_incident(
        views,
        Origin.APP,
        ai=ai,
        run_log=run_log,
        prompt_version="overview-1",
        evidence=LocalEvidenceStore(tmp_path / "cross_store"),
    )

    # Report is invalid due to per-view citation error
    assert report.is_valid is False
    assert any(v2_hash in err for err in report.validation_errors)


def test_per_view_repair_invoked_independently(run_log: RunLog, tmp_path: Path) -> None:
    """When one view has invalid citations, only that view triggers repair; valid view is untouched."""
    v1_vid = synthetic_video(b"view-1-repair")
    v2_vid = synthetic_video(b"view-2-repair")
    v1_hash = sha256_hex(v1_vid)
    v2_hash = sha256_hex(v2_vid)

    # View 1 has invalid timestamp (10.0s > 1.0s synthetic clip duration)
    v1_invalid_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[],
        events=[CitedFact(description="late event", view_id=v1_hash, timestamp_seconds=10.0, confidence=0.9)],
        candidate_collisions=[],
    )
    v1_clean_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[],
        events=[CitedFact(description="fixed event", view_id=v1_hash, timestamp_seconds=0.5, confidence=0.9)],
        candidate_collisions=[],
    )
    v2_valid_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[],
        events=[CitedFact(description="valid event", view_id=v2_hash, timestamp_seconds=0.5, confidence=0.9)],
        candidate_collisions=[],
    )

    ai = SpyAIConnection(
        per_view_facts={v1_hash: v1_invalid_facts, v2_hash: v2_valid_facts},
        repair_facts={v1_hash: v1_clean_facts},
    )

    views = [
        make_view(v1_vid, Role.INTERIOR),
        make_view(v2_vid, Role.EXTERIOR),
    ]

    report = analyse_incident(
        views,
        Origin.APP,
        ai=ai,
        run_log=run_log,
        prompt_version="overview-1",
        evidence=LocalEvidenceStore(tmp_path / "repair_store"),
    )

    # Only View 1 triggered repair
    assert len(ai.received_repairs) == 1
    repaired_req, _, repair_errs = ai.received_repairs[0]
    assert len(repaired_req.views) == 1
    assert repaired_req.views[0].sha256 == v1_hash
    assert any("timestamp" in err.lower() or "duration" in err.lower() for err in repair_errs)

    assert report.is_valid is True
    runs = run_log.list_runs()
    assert len(runs) == 1
    assert runs[0].repair_attempts == 1
    assert runs[0].repair_outcome == "repaired"


@pytest.mark.anyio
async def test_per_file_upload_cap_enforced_by_typed_exception(tmp_path: Path) -> None:
    """A file exceeding max_upload_bytes raises typed UploadTooLarge, even if total size is within max_total."""
    store = LocalEvidenceStore(tmp_path / "per_file_store")
    # Per-file cap 500 bytes; total cap 5000 bytes
    limits = IntakeLimits(max_upload_bytes=500, max_total_upload_bytes=5000)

    # 1. Direct staged_form invocation raises typed UploadTooLarge
    headers, chunks = await make_multipart_boundary_stream(
        [],
        [
            ("interior_files", "file1.mp4", b"x" * 200),
            ("exterior_files", "file2.mp4", b"x" * 800),  # 800 > 500
        ],
    )

    async def receive() -> dict[str, Any]:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "headers": headers}, receive)
    with pytest.raises(UploadTooLarge) as exc_info:
        async with staged_form(req, store, limits):
            pass
    assert "configured size limit" in str(exc_info.value)


@pytest.mark.anyio
async def test_max_files_limit_enforced_by_typed_exception(tmp_path: Path) -> None:
    """Uploading more files than max_files raises typed InvalidUploadForm."""
    store = LocalEvidenceStore(tmp_path / "max_files_store")
    limits = IntakeLimits(max_files=2)

    headers, chunks = await make_multipart_boundary_stream(
        [],
        [
            ("interior_files", "f1.mp4", b"x" * 10),
            ("interior_files", "f2.mp4", b"x" * 10),
            ("exterior_files", "f3.mp4", b"x" * 10),
        ],
    )

    async def receive() -> dict[str, Any]:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "headers": headers}, receive)
    with pytest.raises(InvalidUploadForm) as exc_info:
        async with staged_form(req, store, limits):
            pass
    assert "exceeds the configured limits" in str(exc_info.value)


@pytest.mark.anyio
async def test_max_fields_limit_enforced_by_typed_exception(tmp_path: Path) -> None:
    """Submitting more non-file fields than max_fields raises typed InvalidUploadForm."""
    store = LocalEvidenceStore(tmp_path / "max_fields_store")
    limits = IntakeLimits(max_fields=2)

    headers, chunks = await make_multipart_boundary_stream(
        [("f1", "val1"), ("f2", "val2"), ("f3", "val3")],
        [],
    )

    async def receive() -> dict[str, Any]:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "headers": headers}, receive)
    with pytest.raises(InvalidUploadForm) as exc_info:
        async with staged_form(req, store, limits):
            pass
    assert "exceeds the configured limits" in str(exc_info.value)


@pytest.mark.anyio
async def test_duplicate_text_field_rejected(tmp_path: Path) -> None:
    """Duplicate non-file form keys raise typed InvalidUploadForm."""
    store = LocalEvidenceStore(tmp_path / "dup_fields_store")
    limits = IntakeLimits()

    headers, chunks = await make_multipart_boundary_stream(
        [("interior_label", "label1"), ("interior_label", "label2")],
        [],
    )

    async def receive() -> dict[str, Any]:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "headers": headers}, receive)
    with pytest.raises(InvalidUploadForm) as exc_info:
        async with staged_form(req, store, limits):
            pass
    assert "Send each upload field only once" in str(exc_info.value)


def test_legacy_single_video_upload_with_role(client: TestClient, recordings_dir: Path, run_log: RunLog) -> None:
    """Legacy form submissions with 'video' and 'role' are accepted with backward compatibility."""
    clip = synthetic_video(b"legacy-video-clip")
    clip_hash = sha256_hex(clip)
    input_hash = incident_input_hash([(Role.EXTERIOR, clip_hash)])
    write_recording(recordings_dir, input_hash, PROMPT_VERSION, "Legacy video report summary.")

    files = {"video": ("legacy_front.mp4", clip, "video/mp4")}
    data = {"role": "exterior", "label": "legacy dashcam"}

    response = client.post("/reports", data=data, files=files)
    assert response.status_code == 200

    runs = run_log.list_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run.status is RunStatus.SUCCEEDED
    assert run.report is not None
    assert len(run.report.views) == 1
    assert run.report.views[0].role == Role.EXTERIOR
    assert run.report.views[0].label == "legacy dashcam"


def test_multipart_limits_enforced_for_multi_file_intake(
    client: TestClient, run_log: RunLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Total request body exceeds cap
    monkeypatch.setenv("INCIDENT_MAX_TOTAL_UPLOAD_BYTES", "500")
    monkeypatch.setenv("INCIDENT_MAX_FORM_BYTES", "100")
    evidence = LocalEvidenceStore(tmp_path / "limited_store")
    limited_app = create_app(ai=ReplayAIConnection(), run_log=run_log, evidence=evidence)
    custom_client = authorized_client(limited_app, run_log.database_url)

    # Upload two files where combined size exceeds 500 bytes
    v1 = synthetic_video(b"part1")
    v2 = synthetic_video(b"part2")
    files = [
        ("interior_files", ("v1.mp4", v1, "video/mp4")),
        ("exterior_files", ("v2.mp4", v2, "video/mp4")),
    ]
    res_oversized = custom_client.post("/reports", files=files)
    assert res_oversized.status_code == 422
    assert "size limit" in res_oversized.text

    # 2. Overlong officer question is refused
    res_text = client.post(
        "/reports",
        data={"officer_question": "x" * 500},
        files=[("exterior_files", ("front.mp4", synthetic_video(b"ok"), "video/mp4"))],
    )
    assert res_text.status_code == 422
    assert run_log.list_runs() == []

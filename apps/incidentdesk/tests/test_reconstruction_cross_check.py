"""MIG-04 synthetic saved facts; these are test fixtures, never Gemini output."""

from pathlib import Path

import pytest

from evidence_import_fixtures import FIXTURES
from incident_report.evidence_import import inspect_bundle
from incident_report.evidence_import.models import NormalizedRecord
from incident_report.evidence_import.payloads import EventFields
from incident_report.facts import CollisionObservation, IncidentAssessment, OverviewFacts, PartyFact


def saved_facts(view: str, start: float, end: float) -> OverviewFacts:
    return OverviewFacts(
        parties=[],
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
        incident_assessments=[
            IncidentAssessment(
                view_id=view,
                assessment_note="Synthetic test fixture, not Gemini output",
                camera_role="fixed_camera",
                contact_state="contact_observed",
                contact_interval_start_seconds=start,
                contact_interval_end_seconds=end,
            )
        ],
    )


def test_contact_interval_contains_producer_peak() -> None:
    from incident_report.reconstruction.cross_check import compare_view

    decision = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert decision.envelope
    source = decision.envelope.sources[0]
    event = next(
        r for r in decision.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate"
    )
    assert event.source_time is not None
    facts = saved_facts(source.original_video_sha256, event.source_time, event.source_time)
    rows = compare_view(decision, source.view_id, source.original_video_sha256, facts, {})
    assert rows[0].state == "cannot_compare"  # mapping is mandatory, never inferred
    assert rows[0].producer_frame == event.source_frame


@pytest.mark.parametrize(
    "state,start,end,expected,delta",
    [
        ("contact_observed", 6.0, 6.2, "agree", None),
        ("contact_observed", 4.0, 5.0, "disagree_time", 1.08),
        ("no_contact_observed", None, None, "model_miss", None),
        ("contact_possible_obscured", 6.0, 6.2, "cannot_compare", None),
        ("cannot_assess", None, None, "cannot_compare", None),
    ],
)
def test_contact_time_states(
    state: str, start: float | None, end: float | None, expected: str, delta: float | None
) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    a = f.incident_assessments[0].model_copy(
        update={"contact_state": state, "contact_interval_start_seconds": start, "contact_interval_end_seconds": end}
    )
    f = f.model_copy(update={"incident_assessments": [a]})
    maps = {
        r: c
        for e in d.records
        if isinstance(e.data, EventFields) and e.data.event_type == "collision_candidate"
        for r, c in zip(e.track_refs, ("P1", "P2"), strict=True)
    }
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, maps)
    row = next(r for r in rows if r.item == "Contact time" and r.producer_time == 6.08)
    assert row.state == expected
    if delta is not None:
        assert row.difference_seconds == pytest.approx(delta)
    assert row.video_times == (() if start is None else (start, end))
    assert any(r.state == "cannot_compare" and r.producer_time is None for r in rows)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("mig02-001-05-near-miss-crossing", "tracker_miss"),
        ("mig02-001-06-normal-crossing", "reconstruction_unavailable"),
    ],
)
def test_no_candidate_is_distinct_from_missing_inputs(name: str, expected: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / name)
    assert d.envelope
    s = d.envelope.sources[0]
    rows = compare_view(d, s.view_id, s.original_video_sha256, saved_facts(s.original_video_sha256, 4.0, 5.0), {})
    assert rows[0].state == expected


@pytest.mark.parametrize(
    "other,expected",
    [
        ("Synthetic blue Ford SUV", "agree"),
        ("Synthetic red Honda coupe", "disagree_parties"),
        ("Unknown road user", "cannot_compare"),
    ],
)
def test_party_pair_uses_explicit_codes(other: str, expected: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    names = ["Synthetic white Toyota sedan", "Synthetic blue Ford SUV", "Synthetic red Honda coupe"]
    f = f.model_copy(
        update={
            "parties": [
                PartyFact(
                    description=n, party_code=c, view_id=s.original_video_sha256, timestamp_seconds=0.0, confidence=1.0
                )
                for n, c in zip(names, ("P1", "P2", "P3"), strict=True)
            ],
            "collision_observations": [
                CollisionObservation(
                    kind="vehicle_contact_vehicle",
                    subject=names[0],
                    other_party=other,
                    injury_grade="unknown",
                    view_id=s.original_video_sha256,
                    timestamp_seconds=6.08,
                    confidence=1.0,
                )
            ],
        }
    )
    e = next(e for e in d.records if isinstance(e.data, EventFields) and e.data.event_type == "collision_candidate")
    maps = dict(zip(e.track_refs, ("P1", "P2"), strict=True))
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, maps)
    row = next(r for r in rows if r.item == "Parties in contact" and r.producer_time == 6.08)
    assert row.state == expected
    assert row.video_times == (6.08,)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("moving", "disagree_motion"),
        ("stopped_in_lane", "agree"),
        ("parked_at_kerb", "agree"),
        ("reversing", "cannot_compare"),
        ("unknown", "cannot_compare"),
    ],
)
def test_motion_at_fact_timestamp_and_missing_precontact(value: str, expected: str) -> None:
    from incident_report.facts import StructuredFact
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    event = next(e for e in d.records if isinstance(e.data, EventFields) and e.data.event_type == "collision_candidate")
    ref = event.track_refs[0]

    def movement(kind: str, t: float, key: str) -> NormalizedRecord:
        return event.model_copy(
            update={
                "record_id": key,
                "producer_status": "PROPOSED_BY_SYSTEM",
                "track_refs": (ref,),
                "source_time": t,
                "source_frame": int(t * 25),
                "data": event.data.model_copy(update={"event_id": key, "event_type": kind, "peak_time": t}),
            }
        )

    # Explicit synthetic normalized saved-import variation, no producer fixture edited.
    records = tuple(
        r for r in d.records if not isinstance(r.data, EventFields) or r.data.event_type == "collision_candidate"
    )
    d = d.model_copy(
        update={
            "records": records
            + (
                movement("first_observed", 6.08, "first"),
                movement("stop", 6.08, "stop"),
                movement("start", 8.0, "start"),
                movement("last_observed", 10.0, "last"),
            )
        }
    )
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    party = PartyFact(
        description="Synthetic white Toyota sedan",
        party_code="P1",
        view_id=s.original_video_sha256,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    fact = StructuredFact(
        subject=party.description,
        field="motion_state_before_contact",
        value=value,
        other_party="",
        view_id=s.original_video_sha256,
        timestamp_seconds=7.0,
        confidence=1.0,
    )
    f = f.model_copy(update={"parties": [party], "structured_facts": [fact]})
    maps = dict(zip(event.track_refs, ("P1", "P2"), strict=True))
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, maps)
    motion = next(r for r in rows if r.item == "Motion")
    assert motion.state == expected
    assert motion.video_times == (7.0,)
    assert motion.producer_time == 6.08 and motion.producer_frame == 152
    assert any(r.item == "Pre-contact context" and "Synthetic white Toyota sedan" in r.reason for r in rows)
    later = fact.model_copy(update={"timestamp_seconds": 11.0})
    rows = compare_view(d, s.view_id, s.original_video_sha256, f.model_copy(update={"structured_facts": [later]}), maps)
    assert next(r for r in rows if r.item == "Motion").state == "cannot_compare"
    # An unresolved corrected transition is a barrier, never silently discarded.
    unknown = movement("stop", 7.0, "unknown-stop").model_copy(
        update={"producer_status": "MODIFIED_BY_REVIEWER", "source_time": None, "source_frame": None}
    )
    uncertain = d.model_copy(update={"records": d.records + (unknown,)})
    rows = compare_view(uncertain, s.view_id, s.original_video_sha256, f, maps)
    assert next(r for r in rows if r.item == "Motion").state == "cannot_compare"
    # Gemini can report the missed contact even when the tracker proposes none.
    no_candidates = d.model_copy(
        update={
            "records": tuple(
                r
                for r in d.records
                if not isinstance(r.data, EventFields) or r.data.event_type != "collision_candidate"
            )
        }
    )
    contact = CollisionObservation(
        kind="contact_other",
        subject=party.description,
        other_party="barrier",
        injury_grade="unknown",
        timestamp_seconds=6.08,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    missed = f.model_copy(update={"collision_observations": [contact]})
    rows = compare_view(no_candidates, s.view_id, s.original_video_sha256, missed, maps)
    assert any(r.item == "Pre-contact context" for r in rows)
    own_later = contact.model_copy(update={"timestamp_seconds": 8.0})
    unrelated = contact.model_copy(update={"subject": "Synthetic red Honda coupe", "timestamp_seconds": 2.0})
    separate = missed.model_copy(update={"collision_observations": [own_later, unrelated]})
    rows = compare_view(no_candidates, s.view_id, s.original_video_sha256, separate, maps)
    assert not any(r.item == "Pre-contact context" for r in rows)


def test_cross_check_context_render_is_offline_escaped_and_print_invariant(
    database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json
    import socket
    import uuid

    import psycopg

    from incident_report.domain import Report, ReportView, Role
    from incident_report.reconstruction.config import ReconstructionSettings
    from incident_report.reconstruction.mapping import map_track
    from incident_report.reconstruction.presentation import context
    from incident_report.reconstruction.store import Workspace
    from incident_report.web.templating import build_environment

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    f = saved_facts(s.original_video_sha256, 1.0, 2.0)
    f = f.model_copy(
        update={
            "parties": [
                PartyFact(
                    description=n, party_code=c, view_id=s.original_video_sha256, timestamp_seconds=0.0, confidence=1.0
                )
                for n, c in [
                    ("Synthetic <script>alert(1)</script> white Toyota sedan", "P1"),
                    ("Synthetic blue Ford SUV", "P2"),
                ]
            ]
        }
    )
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="a" * 64,
        prompt_version="synthetic",
        provenance="Synthetic test fixture, not Gemini output",
        summary="Synthetic test fixture",
        views=(
            ReportView(
                filename="fixture.avi",
                label="Front",
                sha256=s.original_video_sha256,
                role=Role.EXTERIOR,
                acquisition_method=None,
            ),
        ),
        facts=f,
    )
    from incident_report.fines import Fine, FinesTable
    from incident_report.rules import evaluate_situation
    from incident_report.rules.models import PartyViolation
    from test_core_fault_rules import lane_situation

    situation = lane_situation()
    fine = FinesTable.default().get("below_minimum_speed")
    assert isinstance(fine, Fine)
    violation = PartyViolation(
        party_description="Synthetic white Toyota sedan",
        rule_id="synthetic fixture",
        item_number=fine.item_number,
        name_en=fine.en,
        aed=fine.aed,
        black_points=fine.black_points,
        impound_days=fine.impound_days,
        source_url=fine.source_url,
        status=fine.status,
        cited_facts=(f.parties[0],),
    )
    report = report.model_copy(update={"parties": evaluate_situation(situation), "violations": (violation,)})
    assert report.parties and report.violations
    saved = report.model_dump_json()
    settings = ReconstructionSettings(True, FIXTURES)
    env = build_environment()
    template = env.get_template("report.html")

    def render(values: dict[str, object], printing: bool = False) -> str:
        return template.render(report=report, print_view=printing, csrf_token=uuid.uuid4().hex, **values)

    before = render({}, True)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,origin,created_at,input_hash,report,status,total_latency_ms) VALUES (%s,'replay',now(),%s,%s::jsonb,'succeeded',0)",
            (report.run_id, report.input_hash, saved),
        )
        conn.commit()
        w = Workspace(conn, settings)
        link = w.attach(report.run_id, "mig02-multi-review", "synthetic reviewer")[0]
        event = next(
            e for e in d.records if isinstance(e.data, EventFields) and e.data.event_type == "collision_candidate"
        )
        for ref, code in zip(event.track_refs, ("P1", "P2"), strict=True):
            map_track(
                w,
                report.run_id,
                link.import_id,
                s.original_video_sha256,
                ref,
                "map",
                code,
                "synthetic reviewer",
                "synthetic fixture identity",
            )

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("network attempted during cross-check render")

        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        values = context(w, report, settings)
        panel = values["reconstruction"][s.original_video_sha256]
        assert any(row.state == "disagree_time" for row in panel.comparisons)
        html = render(values)
        import re

        without_cross_check = render({})
        fault_section = r'<section data-report-section="parties".*?</section>'
        before_fault = re.search(fault_section, without_cross_check, re.S)
        after_fault = re.search(fault_section, html, re.S)
        assert before_fault and after_fault
        assert "Fault Status" in after_fault[0] and "fault-status-text" in after_fault[0]
        assert after_fault[0].encode() == before_fault[0].encode()
        assert "<summary>Reconstruction cross-check</summary>" in html
        assert "<details open><summary>Reconstruction cross-check" not in html
        assert "Reconstruction disagrees with the video analysis on" in html
        assert "Disagrees (time)" in html and "Source frame 152" in html
        assert "<script>alert(1)</script>" not in html
        coarse_contact = CollisionObservation(
            kind="vehicle_contact_vehicle",
            subject=f.parties[0].description,
            other_party=f.parties[1].description,
            injury_grade="unknown",
            view_id=s.original_video_sha256,
            timestamp_seconds=6.0,
            confidence=1.0,
        )
        coarse = report.model_copy(
            update={
                "facts": f.model_copy(update={"incident_assessments": [], "collision_observations": [coarse_contact]})
            }
        )
        coarse_values = context(w, coarse, settings)
        assert coarse_values["reconstruction_disagreements"][s.original_video_sha256] == ()
        assert any(
            r.state == "differs_no_interval"
            for r in coarse_values["reconstruction"][s.original_video_sha256].comparisons
        )
        ambiguous = report.model_copy(
            update={"facts": f.model_copy(update={"collision_observations": [coarse_contact]})}
        )
        assert context(w, ambiguous, settings)["reconstruction_disagreements"][s.original_video_sha256] == ()
        assert render(values, True) == before
        assert context(w, report, ReconstructionSettings(False, FIXTURES)) == {}
        # A later attachment supersedes the old import; old associations are stale.
        import shutil

        root = tmp_path / "exports"
        shutil.copytree(FIXTURES / "mig02-multi-review", root / "new")
        path = root / "new/bundle.json"
        envelope = json.loads(path.read_bytes())
        envelope["created_at"] = "2026-11-05T12:00:00Z"
        path.write_text(json.dumps(envelope))
        newer_settings = ReconstructionSettings(True, root)
        newer = Workspace(conn, newer_settings)
        newer.attach(report.run_id, "new", "synthetic reviewer")
        current_panel = context(newer, report, newer_settings)["reconstruction"][s.original_video_sha256]
        assert current_panel.stale_mappings
        assert all(r.state == "cannot_compare" for r in current_panel.comparisons)
        assert report.model_dump_json() == saved
        assert json.loads(saved)["facts"]["incident_assessments"][0]["assessment_note"].startswith("Synthetic")


def test_fault_inputs_cannot_read_cross_check_or_import_tables() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/incident_report"
    paths = list((root / "rules").rglob("*.py")) + list((root / "ai").rglob("*.py"))
    paths += [root / "questions.py", root / "storyline.py"]
    paths += list((root / "fines").rglob("*.py"))
    paths += [p for p in root.rglob("*.py") if "fine" in p.name]
    forbidden = (
        "reconstruction",
        "cross_check",
        "evidence_import",
        "incident_evidence_imports",
        "track_party_mappings",
        "view_time_alignments",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in forbidden), str(path)


def test_obscured_contact_does_not_become_tracker_miss() -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-001-05-near-miss-crossing")
    assert d.envelope
    s = d.envelope.sources[0]
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    a = f.incident_assessments[0].model_copy(update={"contact_state": "contact_possible_obscured"})
    c = CollisionObservation(
        kind="contact_other",
        subject="Synthetic white Toyota sedan",
        other_party="barrier",
        injury_grade="unknown",
        timestamp_seconds=6.08,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    f = f.model_copy(update={"incident_assessments": [a], "collision_observations": [c]})
    assert compare_view(d, s.view_id, s.original_video_sha256, f, {})[0].state == "cannot_compare"


@pytest.mark.parametrize(
    "status,review,expected",
    [
        ("REJECTED_BY_REVIEWER", False, "cannot_compare"),
        ("CONFIRMED_BY_REVIEWER", True, "cannot_compare"),
        ("CONFIRMED_BY_REVIEWER", False, "agree"),
    ],
)
def test_effective_candidate_and_frame_containment(status: str, review: bool, expected: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    d = d.model_copy(
        update={
            "records": tuple(
                r.model_copy(update={"producer_status": status, "needs_review": review})
                if r.record_id == e.record_id
                else r
                for r in d.records
            )
        }
    )
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    c = CollisionObservation(
        kind="contact_other",
        subject="Synthetic white Toyota sedan",
        other_party="barrier",
        injury_grade="unknown",
        timestamp_seconds=6.081,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    f = f.model_copy(update={"incident_assessments": [], "collision_observations": [c]})
    maps = dict(zip(e.track_refs, ("P1", "P2"), strict=True))
    row = compare_view(d, s.view_id, s.original_video_sha256, f, maps)[0]
    assert row.state == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("mig02-001-01-rear-end", "cannot_compare"),
        ("mig02-001-05-near-miss-crossing", "tracker_miss"),
        ("mig02-001-06-normal-crossing", "reconstruction_unavailable"),
        ("mig02-001-07-occluded-impact", "tracker_miss"),
        ("mig02-multi-review", "cannot_compare"),
        ("mig02-relative", "tracker_miss"),
    ],
)
def test_scripted_exports_with_labelled_synthetic_saved_facts(name: str, expected: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / name)
    assert d.envelope
    s = d.envelope.sources[0]
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    assert compare_view(d, s.view_id, s.original_video_sha256, f, {})[0].state == expected
    other_view = f.model_copy(
        update={"incident_assessments": [a.model_copy(update={"view_id": "b" * 64}) for a in f.incident_assessments]}
    )
    assert all(
        r.state not in {"agree", "disagree_time", "tracker_miss"}
        for r in compare_view(d, s.view_id, s.original_video_sha256, other_view, {})
    )


@pytest.mark.parametrize(
    "name,time,value,expected",
    [
        ("mig02-001-07-occluded-impact", 10.0, "moving", "cannot_compare"),
        ("mig02-001-07-occluded-impact", 10.0, "stopped_in_lane", "cannot_compare"),
        ("mig02-001-07-occluded-impact", 10.72, "moving", "cannot_compare"),
        ("mig02-001-07-occluded-impact", 9.28, "moving", "agree"),
        ("mig02-relative", 3.08, "moving", "cannot_compare"),
        ("mig02-relative", 3.08, "stopped_in_lane", "cannot_compare"),
        ("mig02-relative", 3.2, "stopped_in_lane", "cannot_compare"),
        ("mig02-relative", 2.88, "stopped_in_lane", "agree"),
    ],
)
def test_motion_needs_observation_and_no_gap(name: str, time: float, value: str, expected: str) -> None:
    from incident_report.evidence_import.payloads import TrackFields
    from incident_report.facts import StructuredFact
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / name)
    assert d.envelope
    s = d.envelope.sources[0]
    ref = next(r.track_refs[0] for r in d.records if isinstance(r.data, TrackFields) and r.data.track_id == 1)
    if name == "mig02-relative":
        # Synthetic saved-import variation: explicit stop before the unchanged interpolation span.
        d = d.model_copy(
            update={
                "records": tuple(
                    r.model_copy(
                        update={
                            "source_time": 2.8,
                            "source_frame": 70,
                            "data": r.data.model_copy(update={"peak_time": 2.8}),
                        }
                    )
                    if isinstance(r.data, EventFields) and r.data.event_type == "stop" and ref in r.track_refs
                    else r
                    for r in d.records
                )
            }
        )
    party = PartyFact(
        description="Synthetic white Toyota sedan",
        party_code="P1",
        view_id=s.original_video_sha256,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    contact = CollisionObservation(
        kind="contact_other",
        subject=party.description,
        other_party="barrier",
        injury_grade="unknown",
        timestamp_seconds=11.0,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    fact = StructuredFact(
        subject=party.description,
        field="motion_state_before_contact",
        value=value,
        other_party="",
        view_id=s.original_video_sha256,
        timestamp_seconds=time,
        confidence=1.0,
    )
    f = saved_facts(s.original_video_sha256, 10.8, 11.2).model_copy(
        update={"parties": [party], "collision_observations": [contact], "structured_facts": [fact]}
    )
    row = next(r for r in compare_view(d, s.view_id, s.original_video_sha256, f, {ref: "P1"}) if r.item == "Motion")
    assert row.state == expected
    if expected == "cannot_compare":
        assert row.reason == f"reconstruction has no observation of {party.description} at this time"


@pytest.mark.parametrize(
    "contamination", [None, "incident_report.reconstruction", "incident_report.evidence_import", "incident_report.web"]
)
def test_fault_import_boundary_in_fresh_interpreter(contamination: str | None) -> None:
    import subprocess
    import sys

    script = r"""
import importlib
import pkgutil
import sys
for name in ('rules', 'ai', 'fines'):
    package = importlib.import_module('incident_report.' + name)
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + '.'):
        importlib.import_module(module.name)
for name in ('questions', 'storyline', 'decisions', 'checklist', 'agreement', 'guardrails'):
    importlib.import_module('incident_report.' + name)
if len(sys.argv) > 1:
    importlib.import_module(sys.argv[1])  # explicit contamination self-check, no source edits
prefixes = ('incident_report.reconstruction', 'incident_report.evidence_import', 'incident_report.web')
forbidden = sorted(name for name in sys.modules if any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes))
assert not forbidden, 'Fault input import boundary breached: ' + repr(forbidden)
"""
    args = [sys.executable, "-I", "-c", script]
    if contamination is not None:
        args.append(contamination)
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=60)
    if contamination is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
        assert "Fault input import boundary breached" in result.stderr
        assert contamination in result.stderr


@pytest.mark.parametrize(
    "kind", ["vehicle_contact_vehicle", "vehicle_struck_pedestrian", "contact_other", "assessment", "two_within"]
)
def test_second_contact_is_ambiguous_not_timing_disagreement(kind: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    f = saved_facts(s.original_video_sha256, 4.0, 5.0)
    if kind == "assessment":
        f = f.model_copy(
            update={
                "incident_assessments": f.incident_assessments
                + [
                    f.incident_assessments[0].model_copy(
                        update={"contact_interval_start_seconds": 6.0, "contact_interval_end_seconds": 6.2}
                    )
                ]
            }
        )
    else:
        contact = CollisionObservation.model_validate(
            dict(
                kind="contact_other" if kind == "two_within" else kind,
                subject="Synthetic white Toyota sedan",
                other_party="Synthetic blue Ford SUV",
                injury_grade="unknown",
                timestamp_seconds=6.08,
                view_id=s.original_video_sha256,
                confidence=1.0,
            )
        )
        observations = (
            [contact.model_copy(update={"timestamp_seconds": t}) for t in (4.2, 4.8)]
            if kind == "two_within"
            else [contact]
        )
        f = f.model_copy(update={"collision_observations": observations})
    maps = dict(zip(e.track_refs, ("P1", "P2"), strict=True))
    row = compare_view(d, s.view_id, s.original_video_sha256, f, maps)[0]
    assert row.state == "cannot_compare"
    assert "video analysis reports another contact at" in row.reason
    assert "which contact this is cannot be determined" in row.reason
    assert row.difference_seconds is None


@pytest.mark.parametrize(
    "time,expected,delta",
    [(6.081, "agree", None), (6.0, "differs_no_interval", 0.08), (7.0, "differs_no_interval", -0.92)],
)
def test_no_interval_differences_are_informational(time: float, expected: str, delta: float | None) -> None:
    from types import SimpleNamespace

    from incident_report.reconstruction.cross_check import compare_view
    from incident_report.web.templating import build_environment

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    contact = CollisionObservation(
        kind="contact_other",
        subject="Synthetic white Toyota sedan",
        other_party="barrier",
        injury_grade="unknown",
        timestamp_seconds=time,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    f = saved_facts(s.original_video_sha256, 6.0, 6.2).model_copy(
        update={"incident_assessments": [], "collision_observations": [contact]}
    )
    row = compare_view(d, s.view_id, s.original_video_sha256, f, dict(zip(e.track_refs, ("P1", "P2"), strict=True)))[0]
    assert row.state == expected
    assert row.video_times == (time,) and row.producer_time == 6.08
    if delta is not None:
        assert row.difference_seconds == pytest.approx(delta)
        html = (
            build_environment()
            .get_template("reconstruction_cross_check.html")
            .render(
                panel=SimpleNamespace(comparisons=[row]), view=SimpleNamespace(label="Front", filename="source.avi")
            )
        )
        assert "Times differ (no interval)" in html
        assert "View Front" in html
        assert 'class="meta"' in html


@pytest.mark.parametrize("mode", ["time_mismatch", "outside_agreed_interval", "incomplete_provenance"])
def test_party_comparison_requires_matching_time_and_verified_export(mode: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    f = (
        saved_facts(s.original_video_sha256, 4.0, 5.0)
        if mode == "time_mismatch"
        else saved_facts(s.original_video_sha256, 6.0, 6.2)
    )
    names = ("Synthetic white Toyota sedan", "Synthetic blue Ford SUV")
    parties = [
        PartyFact(description=n, party_code=c, view_id=s.original_video_sha256, timestamp_seconds=0.0, confidence=1.0)
        for n, c in zip(names, ("P1", "P2"), strict=True)
    ]
    contact = CollisionObservation(
        kind="vehicle_contact_vehicle",
        subject=names[0],
        other_party=names[1],
        injury_grade="unknown",
        timestamp_seconds=4.5 if mode == "time_mismatch" else 6.24 if mode == "outside_agreed_interval" else 6.08,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    f = f.model_copy(update={"parties": parties, "collision_observations": [contact]})
    if mode == "incomplete_provenance":
        d = d.model_copy(update={"provenance_state": "incomplete_provenance"})
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, dict(zip(e.track_refs, ("P1", "P2"), strict=True)))
    if mode == "incomplete_provenance":
        assert all(r.state == "cannot_compare" and r.reason == "export provenance incomplete" for r in rows)
    else:
        row = next(r for r in rows if r.item == "Parties in contact" and r.producer_time == 6.08)
        assert row.state == "cannot_compare"
        if mode == "time_mismatch":
            assert row.reason == "contact time does not match"


def test_precontact_ignores_uncertain_candidate_and_explains_missing_inputs() -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    ref = e.track_refs[0]
    records = tuple(
        r.model_copy(update={"needs_review": True})
        if r.record_id == e.record_id
        else r.model_copy(update={"source_time": 7.0, "source_frame": 175})
        if isinstance(r.data, EventFields) and r.data.event_type == "first_observed" and ref in r.track_refs
        else r
        for r in d.records
    )
    d = d.model_copy(update={"records": records})
    party = PartyFact(
        description="Synthetic white Toyota sedan",
        party_code="P1",
        view_id=s.original_video_sha256,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    contact = CollisionObservation(
        kind="contact_other",
        subject=party.description,
        other_party="barrier",
        injury_grade="unknown",
        timestamp_seconds=8.0,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    f = saved_facts(s.original_video_sha256, 8.0, 8.2).model_copy(
        update={"parties": [party], "collision_observations": [contact]}
    )
    assert not any(
        r.item == "Pre-contact context" for r in compare_view(d, s.view_id, s.original_video_sha256, f, {ref: "P1"})
    )
    missing = d.model_copy(update={"missing_inputs": ("calibration_failed",)})
    rows = compare_view(missing, s.view_id, s.original_video_sha256, f, {ref: "P1"})
    assert any(r.item == "Pre-contact context" for r in rows)
    assert all("calibration_failed" not in r.reason for r in rows)
    assert all("calibration failed" in r.reason for r in rows)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("right_pair", "agree"),
        ("wrong_pair", "disagree_parties"),
        ("two_contacts", "cannot_compare"),
        ("same_frame", "agree"),
    ],
)
def test_party_match_uses_agreed_interval_or_frame(mode: str, expected: str) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    names = ("Synthetic white Toyota sedan", "Synthetic blue Ford SUV", "Synthetic red Honda coupe")
    parties = [
        PartyFact(description=n, party_code=c, view_id=s.original_video_sha256, timestamp_seconds=0.0, confidence=1.0)
        for n, c in zip(names, ("P1", "P2", "P3"), strict=True)
    ]
    contact = CollisionObservation(
        kind="vehicle_contact_vehicle",
        subject=names[0],
        other_party=names[2] if mode == "wrong_pair" else names[1],
        injury_grade="unknown",
        timestamp_seconds=6.081 if mode == "same_frame" else 6.1,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    contacts = (
        [contact.model_copy(update={"timestamp_seconds": t}) for t in (6.05, 6.15)]
        if mode == "two_contacts"
        else [contact]
    )
    f = saved_facts(s.original_video_sha256, 6.0, 6.2).model_copy(
        update={"parties": parties, "collision_observations": contacts}
    )
    if mode == "same_frame":
        f = f.model_copy(update={"incident_assessments": []})
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, dict(zip(e.track_refs, ("P1", "P2"), strict=True)))
    row = next(r for r in rows if r.item == "Parties in contact" and r.producer_time == 6.08)
    assert row.state == expected
    if mode == "two_contacts":
        assert row.reason == "which contact cannot be determined"
    else:
        assert row.video_times == (contact.timestamp_seconds,)
        assert names[0] in row.reason and names[1] in row.reason
        if mode == "wrong_pair":
            assert names[2] in row.reason


@pytest.mark.parametrize("same_pair", [True, False])
def test_same_pair_inconsistent_video_times_are_worded_explicitly(same_pair: bool) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    names = ("Synthetic white Toyota sedan", "Synthetic blue Ford SUV", "Synthetic red Honda coupe")
    parties = [
        PartyFact(description=n, party_code=c, view_id=s.original_video_sha256, timestamp_seconds=0.0, confidence=1.0)
        for n, c in zip(names, ("P1", "P2", "P3"), strict=True)
    ]
    contact = CollisionObservation(
        kind="vehicle_contact_vehicle",
        subject=names[0],
        other_party=names[1] if same_pair else names[2],
        injury_grade="unknown",
        timestamp_seconds=6.08,
        view_id=s.original_video_sha256,
        confidence=1.0,
    )
    f = saved_facts(s.original_video_sha256, 4.0, 5.0).model_copy(
        update={"parties": parties, "collision_observations": [contact]}
    )
    row = compare_view(d, s.view_id, s.original_video_sha256, f, dict(zip(e.track_refs, ("P1", "P2"), strict=True)))[0]
    assert row.state == "cannot_compare"
    assert row.video_times == (4.0, 5.0, 6.08)
    if same_pair:
        assert row.reason == "video analysis contact times are inconsistent"
    else:
        assert row.reason.startswith("video analysis reports another contact at")


@pytest.mark.parametrize("extra_time", [3.0, 8.0])
@pytest.mark.parametrize("valid_extra", [True, False])
def test_additional_reconstruction_contact_is_not_disagreement(extra_time: float, valid_extra: bool) -> None:
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    e = next(r for r in d.records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate")
    # Labelled synthetic saved-import variation; immutable producer fixture untouched.
    extra = e.model_copy(
        update={
            "record_id": "synthetic-extra-contact",
            "source_time": extra_time,
            "source_frame": int(extra_time * 25),
            "producer_status": "CONFIRMED_BY_REVIEWER" if valid_extra else "REJECTED_BY_REVIEWER",
            "data": e.data.model_copy(update={"event_id": "synthetic-extra-contact", "peak_time": extra_time}),
        }
    )
    d = d.model_copy(update={"records": d.records + (extra,)})
    f = saved_facts(s.original_video_sha256, 6.0, 6.2)
    maps = dict(zip(e.track_refs, ("P1", "P2"), strict=True))
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, maps)
    time_rows = [r for r in rows if r.item == "Contact time"]
    assert next(r for r in time_rows if r.producer_time == 6.08).state == "agree"
    additional = next(r for r in time_rows if r.producer_time == extra_time)
    assert additional.state == "cannot_compare"
    assert additional.difference_seconds is None
    if valid_extra:
        assert additional.reason == "reconstruction reports an additional contact"
    else:
        assert "additional contact" not in additional.reason
    # Without a matching candidate, preserve the original single-contact timing disagreement.
    f = saved_facts(s.original_video_sha256, 4.0, 5.0)
    rows = compare_view(d, s.view_id, s.original_video_sha256, f, maps)
    assert next(r for r in rows if r.item == "Contact time" and r.producer_time == 6.08).state == "disagree_time"


@pytest.mark.parametrize("state", ["lost", "interpolated"])
@pytest.mark.parametrize(
    "gap_time,expected",
    [
        (6.6, "cannot_compare"),
        (6.44, "cannot_compare"),
        (7.0, "cannot_compare"),
        (None, "cannot_compare"),
        (6.3, "agree"),
        (7.2, "agree"),
    ],
)
def test_frameless_gap_cannot_be_ignored(state: str, gap_time: float | None, expected: str) -> None:
    from incident_report.evidence_import.payloads import TrackFields
    from incident_report.facts import StructuredFact
    from incident_report.reconstruction.cross_check import compare_view

    d = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert d.envelope
    s = d.envelope.sources[0]
    observation = next(
        r for r in d.records if isinstance(r.data, TrackFields) and r.data.track_id == 1 and r.source_time == 7.0
    )
    ref = observation.track_refs[0]
    # Constructed test record, not a rewritten producer fixture or inferred frame.
    gap = observation.model_copy(
        update={
            "record_id": "synthetic-frameless-gap",
            "source_frame": None,
            "source_time": gap_time,
            "data": observation.data.model_copy(update={"state": state}),
        }
    )
    d = d.model_copy(update={"records": d.records + (gap,)})
    party = PartyFact(
        description="Synthetic white Toyota sedan",
        party_code="P1",
        view_id=s.original_video_sha256,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    fact = StructuredFact(
        subject=party.description,
        field="motion_state_before_contact",
        value="stopped_in_lane",
        other_party="",
        view_id=s.original_video_sha256,
        timestamp_seconds=7.0,
        confidence=1.0,
    )
    f = saved_facts(s.original_video_sha256, 6.0, 6.2).model_copy(
        update={"parties": [party], "structured_facts": [fact]}
    )
    row = next(r for r in compare_view(d, s.view_id, s.original_video_sha256, f, {ref: "P1"}) if r.item == "Motion")
    assert row.state == expected
    if expected == "cannot_compare":
        assert row.reason == f"reconstruction has no observation of {party.description} at this time"

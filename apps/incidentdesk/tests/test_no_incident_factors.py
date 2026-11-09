"""RPT-26 at Seam 3 and the Report/rendering interface: no prose decisions."""

from typing import Any
from uuid import uuid4

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIResponse
from incident_report.ai.dense import CandidateWindow, DenseSettings
from incident_report.domain import Origin, Report
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact, StructuredFact
from incident_report.fines import FinesTable
from incident_report.model_calls import ModelCall
from incident_report.rules import FaultStatus, evaluate_situation, situation_from_overview
from incident_report.rules.violations import evaluate_violations
from incident_report.runlog import RunLog
from incident_report.web.templating import build_environment
from video_fixtures import synthetic_video

A = "white Toyota sedan"
B = "red Nissan sedan"


def citation(description: str, timestamp: float = 1.0) -> CitedFact:
    return CitedFact(description=description, view_id="road", timestamp_seconds=timestamp, confidence=0.9)


def facts(*, collision: bool = False, signal: str = "red", phase_t: float = 1.0) -> OverviewFacts:
    records = []
    values = [
        ("scene", "officer_directing_traffic", "no", ""),
        ("scene", "conflict_zone_type", "signalised_junction", ""),
    ]
    for name, other, phase, relation in ((A, B, signal, "B_from_A_left"), (B, A, "green", "B_from_A_right")):
        values.extend(
            [
                (name, "party_type", "car", ""),
                (name, "movement", "straight", ""),
                (name, "approach_control_faced", "signal", ""),
                (name, "signal_state_when_crossing_stop_line", phase, ""),
                (name, "relative_position_of_other", relation, other),
            ]
        )
    for subject, field, value, other in values:
        records.append(
            StructuredFact.model_validate(
                dict(
                    subject=subject,
                    field=field,
                    value=value,
                    other_party=other,
                    view_id="road",
                    timestamp_seconds=phase_t if field == "signal_state_when_crossing_stop_line" else 1.0,
                    confidence=0.9,
                )
            )
        )
    return OverviewFacts(
        parties=[PartyFact(**citation(name).model_dump()) for name in (A, B)],
        events=[],
        candidate_collisions=[citation("contact", 3.0)] if collision else [],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=records,
    )


def report_for(observed: OverviewFacts) -> Report:
    scene = situation_from_overview(observed)
    return Report(
        run_id=uuid4(),
        input_hash="a" * 64,
        prompt_version="overview-2",
        provenance="synthetic",
        summary="Recorded observations.",
        views=(),
        facts=observed,
        parties=evaluate_situation(scene),
        violations=evaluate_violations(scene),
    )


def test_red_crossing_without_collision_is_cited_and_fined() -> None:
    scene = situation_from_overview(facts())
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in evaluate_situation(scene))
    violations = evaluate_violations(scene)
    assert len(violations) == 1
    violation = violations[0]
    row = FinesTable.default().get("red_light_light")
    assert violation.party_description == A
    assert violation.item_number == "33a"
    assert violation.aed == row.aed
    assert violation.black_points == row.black_points
    assert any("signal_state_when_crossing_stop_line" in c.description for c in violation.cited_facts)


@pytest.mark.parametrize(
    "collision,signal,expected",
    [
        (False, "green", "No Incident"),
        (False, "red", "No collision"),
        (True, "green", "Collision"),
        (True, "red", "Collision"),
    ],
)
def test_incident_label_depends_on_collisions_and_violations(collision: bool, signal: str, expected: str) -> None:
    report = report_for(facts(collision=collision, signal=signal))
    assert report.incident_status == expected
    html = build_environment().get_template("report.html").render(report=report)
    assert f'data-incident-status="{expected}"' in html
    if not collision and signal == "red":
        assert "Violations and fines" in html
        assert "33a" in html
        assert "No Incident" not in html


@pytest.mark.parametrize("update", [{"is_valid": False}, {"facts": None}])
def test_missing_or_invalid_facts_do_not_claim_no_incident(update: dict[str, Any]) -> None:
    report = report_for(facts(signal="green")).model_copy(update=update)
    assert report.incident_status == "Cannot Determine"


def test_dense_collision_also_prevents_no_incident() -> None:
    report = report_for(facts(signal="green")).model_copy(update={"dense_facts": (facts(collision=True),)})
    assert report.incident_status == "Collision"


def test_noncausal_breach_is_a_separate_cited_factor_and_cannot_decide_fault() -> None:
    report = report_for(facts(collision=True, phase_t=4.0))
    before = tuple(p.status for p in report.parties)
    assert before == (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)
    factors = report.contributing_factors
    assert len(factors) == 1
    assert factors[0].classification == "Contributing Factor"
    assert factors[0].party_description == A
    assert factors[0].article == "MR 130/1997 Art. 25(1)"
    assert factors[0].cited_facts
    assert tuple(p.status for p in report.parties) == before
    assert report.violations == ()
    html = build_environment().get_template("report.html").render(report=report)
    assert '<section aria-label="Contributing Factors">' in html
    assert factors[0].article in html
    assert factors[0].source_url in html
    assert "road @ 4.0s" in html
    restored = Report.model_validate_json(report.model_dump_json())
    assert [f.model_dump() for f in restored.contributing_factors] == [f.model_dump() for f in factors]


@pytest.mark.parametrize("collision", [False, True])
def test_prose_behaviour_is_only_a_cited_observation(collision: bool) -> None:
    observed = facts(collision=collision, signal="green")
    baseline = report_for(observed)
    behaviour = citation("<script>eyes off road; crossed red light; no indicator</script>")
    report = report_for(observed.model_copy(update={"behaviour_observations": [behaviour]}))
    assert report.contributing_factors == ()
    assert report.violations == baseline.violations == ()
    assert tuple(p.status for p in report.parties) == tuple(p.status for p in baseline.parties)
    html = build_environment().get_template("report.html").render(report=report)
    assert "Behaviour observations" in html
    assert "&lt;script&gt;eyes off road" in html
    assert "<script>eyes off road" not in html
    assert "road @ 1.0s" in html
    assert '<section aria-label="Contributing Factors">' not in html


class RecordedSignals:
    """Seam-2 synthetic answers only; real entry point, rules, validation and RunLog."""

    def __init__(self, signals: list[str], *, collision: bool = False) -> None:
        self.signals = iter(signals)
        self.collision = collision

    def analyse(self, request: AIRequest) -> AIResponse:
        payload = facts(
            signal=next(self.signals), collision=self.collision, phase_t=4.0 if self.collision else 1.0
        ).model_dump()
        for group in payload.values():
            for record in group:
                record["view_id"] = request.views[0].sha256
                record["timestamp_seconds"] = (
                    (0.8 if record.get("field") == "signal_state_when_crossing_stop_line" else 0.2)
                    if self.collision
                    else 0.5
                )
        if self.collision:
            payload["candidate_collisions"][0]["timestamp_seconds"] = 0.4
        return AIResponse(
            summary="Synthetic signal observations.",
            provenance="synthetic",
            facts=OverviewFacts.model_validate(payload),
        )


@pytest.mark.parametrize(
    "signals,count,status",
    [
        (["red"], 1, "No collision"),
        (["green"], 0, "No Incident"),
        (["red", "green"], 0, "Cannot Determine"),
        (["green", "red", "red"], 1, "No collision"),
        (["red", "unknown", "green"], 0, "Cannot Determine"),
    ],
)
def test_entry_no_collision_violation_and_agreement(
    signals: list[str], count: int, status: str, run_log: RunLog
) -> None:
    report = analyse_incident(
        [make_view(synthetic_video(b"no-collision-signal"))],
        Origin.REPLAY,
        ai=RecordedSignals(signals),
        run_log=run_log,
        prompt_version="overview-2",
        agreement_settings=AgreementSettings(n_runs=len(signals)),
    )
    assert report.is_valid
    assert len(report.violations) == count
    assert report.incident_status == status
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in report.parties)
    saved = run_log.get(report.run_id).report
    assert saved is not None
    assert saved.incident_status == status
    assert saved.violations == report.violations
    if count:
        assert report.violations[0].item_number == "33a"
        assert report.violations[0].agreement_status == ("not_measured" if len(signals) == 1 else "flagged")


@pytest.mark.parametrize(
    "field,subject",
    [
        ("party_type", A),
        ("movement", A),
        ("approach_control_faced", A),
        ("signal_state_when_crossing_stop_line", A),
        ("officer_directing_traffic", "scene"),
    ],
)
def test_standalone_red_requires_each_typed_observation(field: str, subject: str) -> None:
    observed = facts()
    records = [
        record.model_copy(update={"value": "unknown"})
        if record.subject == subject and record.field == field
        else record
        for record in observed.structured_facts
    ]
    assert evaluate_violations(situation_from_overview(observed.model_copy(update={"structured_facts": records}))) == ()


@pytest.mark.parametrize(
    "signal", ["unknown", "not_visible", "yellow", "green_arrow", "flashing_yellow", "signal_dark", "not_applicable"]
)
def test_standalone_uncovered_phase_has_no_fine(signal: str) -> None:
    assert evaluate_violations(situation_from_overview(facts(signal=signal))) == ()


def test_standalone_signal_cannot_be_combined_across_disjoint_view_clocks() -> None:
    observed = facts()
    records = [
        r.model_copy(update={"view_id": "another_clock"})
        if r.subject == A and r.field == "signal_state_when_crossing_stop_line"
        else r
        for r in observed.structured_facts
    ]
    assert evaluate_violations(situation_from_overview(observed.model_copy(update={"structured_facts": records}))) == ()


def test_cross_view_signal_disagreement_never_charges_a_fine() -> None:
    observed = facts()
    phase = next(
        r for r in observed.structured_facts if r.subject == A and r.field == "signal_state_when_crossing_stop_line"
    )
    scene = situation_from_overview(
        observed.model_copy(
            update={
                "structured_facts": [
                    *observed.structured_facts,
                    phase.model_copy(update={"value": "green", "view_id": "other_view"}),
                ]
            }
        )
    )
    assert scene.contradictions
    assert evaluate_violations(scene) == ()


@pytest.mark.parametrize("signal", ["unknown", "not_visible", "yellow"])
def test_unresolved_crossing_never_claims_no_incident(signal: str) -> None:
    assert report_for(facts(signal=signal)).incident_status == "Cannot Determine"


@pytest.mark.parametrize("signals", [["unknown"], ["unknown", "unknown", "unknown"]])
def test_all_abstaining_runs_do_not_claim_no_incident(signals: list[str], run_log: RunLog) -> None:
    report = analyse_incident(
        [make_view(synthetic_video(b"unknown-crossing"))],
        Origin.REPLAY,
        ai=RecordedSignals(signals),
        run_log=run_log,
        prompt_version="overview-2",
        agreement_settings=AgreementSettings(n_runs=len(signals)),
    )
    assert report.violations == ()
    assert report.incident_status == "Cannot Determine"


def test_unsupported_turning_crossing_never_claims_no_incident() -> None:
    observed = facts(signal="red")
    records = [
        r.model_copy(update={"value": "left_turn"}) if r.subject == A and r.field == "movement" else r
        for r in observed.structured_facts
    ]
    assert report_for(observed.model_copy(update={"structured_facts": records})).incident_status == "Cannot Determine"


@pytest.mark.parametrize(
    "signals,agreement",
    [
        (["red"], "not_measured"),
        (["red", "red", "red"], "trusted"),
        (["red", "red", "green"], "flagged"),
        (["red", "green", "green"], "cannot_determine"),
    ],
)
def test_contributing_findings_have_independent_agreement(signals: list[str], agreement: str, run_log: RunLog) -> None:
    report = analyse_incident(
        [make_view(synthetic_video(b"contributing-breach"))],
        Origin.REPLAY,
        ai=RecordedSignals(signals, collision=True),
        run_log=run_log,
        prompt_version="overview-2",
        agreement_settings=AgreementSettings(n_runs=len(signals)),
    )
    assert report.is_valid
    assert report.incident_status == "Collision"
    assert report.violations == ()
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in report.parties)
    assert len(report.contributing_factors) == 1
    factor = report.contributing_factors[0]
    assert factor.agreement_status == agreement
    html = build_environment().get_template("report.html").render(report=report)
    assert ('data-factor-agreement="' + agreement + '"') in html
    if agreement in {"flagged", "cannot_determine"}:
        assert "Check" in html
    restored = run_log.get(report.run_id).report
    assert restored is not None
    assert restored.contributing_factors[0].agreement_status == agreement


def test_abstention_citations_cannot_supply_same_view_support_for_a_red_crossing() -> None:
    observed = facts()
    phase = next(
        r for r in observed.structured_facts if r.subject == A and r.field == "signal_state_when_crossing_stop_line"
    )
    records = [
        r.model_copy(update={"view_id": "red_only_camera"}) if r is phase else r for r in observed.structured_facts
    ]
    records.append(phase.model_copy(update={"value": "unknown"}))
    scene = situation_from_overview(observed.model_copy(update={"structured_facts": records}))
    assert scene.parties[0].signal_state_when_crossing_stop_line.value == "red"
    assert len(scene.parties[0].signal_state_when_crossing_stop_line.evidence) == 2
    assert evaluate_violations(scene) == ()


class RecordedDenseSignal(RecordedSignals):
    def analyse_window(
        self, request: AIRequest, window: CandidateWindow
    ) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
        response = RecordedSignals(["red"], collision=True).analyse(request)
        assert response.facts is not None
        return response.facts, ()


def test_dense_only_contributing_factor_survives_with_citations(run_log: RunLog) -> None:
    report = analyse_incident(
        [make_view(synthetic_video(b"dense-only-factor"))],
        Origin.REPLAY,
        ai=RecordedDenseSignal(["unknown"], collision=True),
        run_log=run_log,
        prompt_version="overview-2",
        agreement_settings=AgreementSettings(n_runs=1),
        dense_settings=DenseSettings(model="gemini-2.5-flash"),
    )
    assert report.is_valid
    assert len(report.contributing_factors) == 1
    assert report.contributing_factors[0].agreement_status == "not_measured"
    assert report.contributing_factors[0].cited_facts
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in report.parties)
    saved = run_log.get(report.run_id).report
    assert saved is not None and saved.contributing_factors


def test_mid_block_confirmed_no_signal_is_no_incident() -> None:
    payload = facts(signal="not_applicable").model_dump()
    payload["parties"] = payload["parties"][:1]
    payload["structured_facts"] = [
        dict(record, value="mid_block" if record["field"] == "conflict_zone_type" else "none_confirmed")
        for record in payload["structured_facts"]
        if record["field"] in {"conflict_zone_type", "approach_control_faced"} and record["subject"] in {"scene", A}
    ]
    report = report_for(OverviewFacts.model_validate(payload))
    assert report.is_valid
    assert report.violations == ()
    assert report.incident_status == "No Incident"


@pytest.mark.parametrize(
    "control,phase",
    [("signal", "unknown"), ("signal", "not_visible"), ("not_in_view", "unknown"), (None, "unknown")],
)
def test_mid_block_does_not_resolve_missing_or_unseen_signal(control: str | None, phase: str) -> None:
    payload = facts(signal=phase).model_dump()
    for record in payload["structured_facts"]:
        if record["field"] == "conflict_zone_type":
            record["value"] = "mid_block"
        if record["subject"] == A and record["field"] == "approach_control_faced":
            record["value"] = control
    if control is None:
        payload["structured_facts"] = [r for r in payload["structured_facts"] if r["value"] is not None]
    report = report_for(OverviewFacts.model_validate(payload))
    assert report.violations == ()
    assert report.incident_status == "Cannot Determine"


def test_confirmed_no_signal_cannot_hide_another_partys_unseen_signal() -> None:
    payload = facts(signal="unknown").model_dump()
    for record in payload["structured_facts"]:
        if record["subject"] == B and record["field"] == "approach_control_faced":
            record["value"] = "none_confirmed"
    assert report_for(OverviewFacts.model_validate(payload)).incident_status == "Cannot Determine"


def test_conflicting_no_signal_and_signal_evidence_still_abstains() -> None:
    payload = facts(signal="unknown").model_dump()
    control = next(
        r for r in payload["structured_facts"] if r["subject"] == A and r["field"] == "approach_control_faced"
    )
    payload["structured_facts"].append(dict(control, value="none_confirmed", view_id="other_view"))
    assert report_for(OverviewFacts.model_validate(payload)).incident_status == "Cannot Determine"


def test_confirmed_no_signal_collision_remains_collision() -> None:
    payload = facts(collision=True, signal="unknown").model_dump()
    for record in payload["structured_facts"]:
        if record["field"] == "approach_control_faced":
            record["value"] = "none_confirmed"
    observed = OverviewFacts.model_validate(payload)
    report = report_for(observed)
    assert report.incident_status == "Collision"
    assert report.violations == ()
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in report.parties)

"""Walk the Report contract recursively; omitted data needs an explicit reason.

Each populated scalar is changed independently and must change the rendered page.
The schema walk also catches future fields absent from this fixture.
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from incident_report.agreement import AgreementResult, AgreementStatus, FieldAgreement
from incident_report.domain import Report
from incident_report.facts import (
    ChecklistParty,
    CitedFact,
    CollisionObservation,
    DriverObservation,
    DrowsinessObservation,
    IncidentAssessment,
    ObservationNote,
    OverviewFacts,
    PartyFact,
    StructuredFact,
    TextReading,
)
from incident_report.rules.consistency import ConsistencyIssue
from incident_report.rules.driver import DriverAssessment
from incident_report.rules.models import FaultStatus, Finding, PartyFault, PartyViolation
from incident_report.web.templating import build_environment
from test_invalid_report_rendering import make_report

# Not shown, because these are internal records, duplicates, or non-decision metrics.
# Exact model.field entries: never wildcard a model, so new schema fields fail closed.
NOT_SHOWN = {
    "Report.stage_latencies_ms": "not shown, because timings belong to the Run log, not video findings",
    "Report.overview_collision_recall": "not shown, because benchmark recall is not an incident finding",
    "Report.dense_facts": "not shown, because consolidated facts already retain dense evidence; avoid duplicates",
    "ReportView.session_id": "not shown, because anonymous session attribution is internal access metadata",
    "ReportView.ffprobe_stdout": "not shown, because raw probe diagnostics belong to evidence storage",
    "ReportView.ffprobe_stderr": "not shown, because raw probe diagnostics belong to evidence storage",
    "ReportView.ffprobe_stdout_object": "not shown, because probe object lineage belongs to evidence storage",
    "ReportView.ffprobe_stderr_object": "not shown, because probe object lineage belongs to evidence storage",
    "ReportView.working_copy": "not shown, because derived-object lineage belongs to evidence storage",
    "CitedFact.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "PartyFact.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "StructuredFact.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "TextReading.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "DriverObservation.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "DrowsinessObservation.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "CollisionObservation.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "ChecklistParty.confidence": "not shown, because extractor confidence is not measured accuracy or agreement",
    "PartyFault.rule_id": "not shown, because the official article and source identify the rule for the officer",
    "PartyFault.pair_selection": "not shown, because the party reason already names the pair and cites its observations; the typed record is for reviewers",
    "Finding.rule_id": "not shown, because the official article and source identify the rule for the officer",
    "PartyViolation.rule_id": "not shown, because the official item and source identify the violation",
    "PartyViolation.research_reference": "not shown, because the officer gets the official source and notes",
    "FieldAgreement.agreed_value": "not shown, because raw voting encodings duplicate the displayed findings",
    "FieldAgreement.values": "not shown, because raw voting encodings duplicate the agreement explanation",
    "AgreementResult.total_latency_ms": "not shown, because latency belongs to the Run log",
    "AgreementResult.model_call_count": "not shown, because call accounting belongs to the Run log",
    "CollisionObservation.source_part": "not shown, because the Report cites full-clip times; the video part is a record for reviewers",
    "CollisionObservation.part_time_seconds": "not shown, because the Report cites full-clip times; the raw part time is a record for reviewers",
    "CollisionObservation.part_fps": "not shown, because it is the sampling rate of the video part, a record for reviewers of why a pair was or was not confirmed",
    "IncidentAssessment.contact_interval_part": "not shown, because the Report cites full-clip times; the video part is a record for reviewers",
    "IncidentAssessment.detail_window_start_seconds": "not shown, because the window is a request setting recorded for reviewers",
    "IncidentAssessment.detail_window_end_seconds": "not shown, because the window is a request setting recorded for reviewers",
    "IncidentAssessment.recovery_triggers": "not shown, because the officer sees the outcome line and a failed check as a consistency issue",
}


def full_report() -> Report:
    report = make_report(is_valid=False, summary="Synthetic parity summary", errors=("Synthetic validation error",))
    key = report.views[0].sha256
    fact = CitedFact(description="Synthetic cited observation", view_id=key, timestamp_seconds=2.0, confidence=0.9)
    party = PartyFact(**fact.model_dump(), color="silver", make_model="Toyota", body_type="sedan", party_code="P1")
    structured = StructuredFact(
        subject=party.description,
        field="movement",
        value="straight",
        other_party="",
        view_id=key,
        timestamp_seconds=2.0,
        confidence=0.9,
    )
    finding = Finding(
        party_description=party.description,
        rule_id="synthetic-rule",
        legal_source="Synthetic source",
        article="Synthetic article",
        source_url="/synthetic",
        reason="Synthetic finding reason",
        classification="Finding",
        agreement_status="flagged",
        agreement_detail="Synthetic check detail",
        cited_facts=(fact,),
    )
    fault = PartyFault(
        party_description=party.description,
        status=FaultStatus.CANNOT_DETERMINE,
        color="grey",
        rule_id="synthetic-rule",
        legal_source="Synthetic source",
        article="Synthetic article",
        reason="Synthetic fault reason",
        cited_facts=(fact,),
        agreement_status="flagged",
        agreement_detail="Synthetic fault check",
        findings=(finding,),
    )
    violation = PartyViolation(
        party_description=party.description,
        rule_id="synthetic-rule",
        item_number="fixture",
        name_en="Synthetic violation",
        name_ar="نص اختباري",
        aed=1234,
        black_points=3,
        impound_days=2,
        source_url="/synthetic",
        status="provisional",
        cited_facts=(fact,),
        notes="Synthetic notes",
        research_reference="fixture",
        agreement_status="flagged",
        agreement_detail="Synthetic violation check",
    )
    field = FieldAgreement(
        field_name="fixture",
        status=AgreementStatus.FLAGGED,
        agreed_value=True,
        runs_total=3,
        runs_agreeing=2,
        details="Synthetic agreement detail",
        values=[True, True, False],
    )
    observation = DriverObservation(  # synthetic-fixture
        kind="looking_at_phone",
        subject=party.description,
        start_seconds=1.0,
        end_seconds=3.0,
        confidence=0.8,
        view_id=key,
    )
    collision_observation = CollisionObservation(  # synthetic-fixture
        kind="person_injured_visible",
        subject=party.description,
        other_party="",
        injury_grade="serious",
        view_id=key,
        timestamp_seconds=2.0,
        confidence=0.8,
        visibility="visible",
        source_part="whole_clip",
        part_time_seconds=2.0,
    )
    checklist_row = ChecklistParty(  # synthetic-fixture
        party_code="P1",
        first_seen_seconds=1.0,
        attributes_seen_at_seconds=1.5,
        movement_seen_at_seconds=2.0,
        party_type="other",
        party_type_other="tractor",
        marking="none_seen",
        colour_family="other",
        colour_other="teal",
        manoeuvre="other",
        manoeuvre_other="reversing out",
        turn_direction="left",
        roundabout_exit_position="not_applicable",
        road_class_on_approach="side_road",
        road_class_basis="short_or_ends_at_junction",
        confidence=0.8,
        view_id=key,
        display_name="P1 teal tractor",
    )
    observation_note = ObservationNote(  # synthetic-fixture
        party_code="P1", timestamp_seconds=2.0, note="Synthetic note", view_id=key, subject="P1 teal tractor"
    )
    incident_assessment = IncidentAssessment(  # synthetic-fixture
        assessment_note="Synthetic assessment note",
        camera_role="unknown",
        contact_state="contact_possible_obscured",
        contact_interval_start_seconds=1.0,
        contact_interval_end_seconds=3.0,
        view_id=key,
        contact_interval_part="whole_clip",
        detail_window_start_seconds=1.0,
        detail_window_end_seconds=5.0,
        recovery_triggers=["negative_state"],
        recovery="agreed_no_contact",
    )
    assessment = DriverAssessment(  # synthetic-fixture
        party_description=party.description,
        phone_use="GPS",
        reason="Synthetic driver assessment reason",
        cited_facts=(observation.citation(),),
    )
    return report.model_copy(
        update={
            "guardrail_results": {"citations": ("Synthetic guardrail error",)},
            "consistency_issues": (
                ConsistencyIssue(
                    code="synthetic-check",
                    party_key=("synthetic-party-key", "synthetic-view", "synthetic-name"),
                    subject=party.description,
                    fields=("movement", "roundabout_entry_state"),
                    reason="Synthetic consistency reason",
                    cited_facts=(fact,),
                ),
            ),
            "overview_consistency_issues": (
                (
                    ConsistencyIssue(
                        code="synthetic-overview-check",
                        party_key=("synthetic-overview-party", "synthetic-overview-view", "synthetic-overview-name"),
                        subject=party.description,
                        fields=("movement",),
                        reason="Synthetic overview consistency reason",
                        cited_facts=(fact,),
                    ),
                ),
            ),
            "driver_assessments": (assessment,),
            "views": (
                report.views[0].model_copy(
                    update={
                        "uploaded_at": datetime(2026, 1, 1, tzinfo=UTC),
                        "size_bytes": 123,
                        "mime_type": "video/mp4",
                        "validation_error": "Synthetic view error",
                    }
                ),
            ),
            "facts": OverviewFacts(
                parties=[party],
                unmatched_dense_parties=[
                    party.model_copy(update={"description": "Additional silver sedan observation"})
                ],
                events=[fact],
                candidate_collisions=[fact],
                behaviour_observations=[fact],
                location_conditions=[fact],
                structured_facts=[structured],
                driver_observations=[observation],
                drowsiness_observations=[
                    DrowsinessObservation(  # synthetic-fixture
                        kind="eye_closure",
                        subject=party.description,
                        view_id=key,
                        timestamp_seconds=2.0,
                        start_seconds=1.0,
                        end_seconds=3.0,
                        confidence=0.8,
                    )
                ],
                collision_observations=[collision_observation],
                party_checklist=[checklist_row],
                observation_notes=[observation_note],
                incident_assessments=[incident_assessment],
                text_readings=[
                    TextReading(
                        kind="overlay_time",
                        text="12:34",
                        language="en",
                        view_id=key,
                        timestamp_seconds=2.0,
                        confidence=0.9,
                    )
                ],
            ),
            "parties": (fault,),
            "violations": (violation,),
            "agreement": AgreementResult(n_runs=3, status=AgreementStatus.FLAGGED, fields={"fixture": field}),
        }
    )


def schema_fields(model: type[BaseModel]) -> set[str]:
    result = set()
    for name, field in model.model_fields.items():
        key = f"{model.__name__}.{name}"
        if key in NOT_SHOWN:
            continue
        result.add(key)
        pending = [field.annotation]
        while pending:
            annotation = pending.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                result.update(schema_fields(annotation))
            else:
                pending.extend(get_args(annotation))
    return result


def variations(value: Any) -> Any:
    """Yield (field keys covered, a copy with one scalar changed). No model/rule edits."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            key = f"{type(value).__name__}.{name}"
            if key in NOT_SHOWN:
                continue
            original = getattr(value, name)
            for covered, changed in variations(original):
                yield covered | {key}, value.model_copy(update={name: changed})
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            for covered, changed in variations(item):
                items = list(value)
                items[index] = changed
                yield covered, tuple(items) if isinstance(value, tuple) else items
    elif isinstance(value, dict):
        for key, item in value.items():
            for covered, changed in variations(item):
                yield covered, {**value, key: changed}
    elif value is not None:
        if isinstance(value, Enum):
            changed = next(member for member in type(value) if member != value)
        elif isinstance(value, bool):
            changed = not value
        elif isinstance(value, (int, float)):
            changed = value + 7
        elif isinstance(value, datetime):
            changed = value.replace(year=2025)
        elif isinstance(value, str):
            changed = value + " parity"
        else:
            # UUID (Report.run_id).
            import uuid

            changed = uuid.UUID(int=1)
        yield set(), changed


@pytest.mark.parametrize("review", [False, True])
def test_every_report_field_is_rendered_or_has_an_explicit_omission_reason(review: bool) -> None:
    template = build_environment().get_template("report.html")
    report = full_report()
    baseline = template.render(report=report, review_workspace=review)
    covered: set[str] = set()
    failures = []
    for keys, changed in variations(report):
        # Keep rule reconciliation outside this display test. Mutating a party's
        # name independently of its references deliberately violates input integrity.
        class DisplayReport:
            contradictions = report.contradictions

            def __init__(self, data: Report) -> None:
                self.data = data

            def __getattr__(self, name: str) -> Any:
                return getattr(self.data, name)

        if template.render(report=DisplayReport(changed), review_workspace=review) == baseline:
            failures.append(keys)
        covered.update(keys)
    assert not failures, f"Fields lost during rendering: {failures}"
    assert schema_fields(Report) <= covered, f"Fixture must exercise: {schema_fields(Report) - covered}"
    assert all(reason.startswith("not shown, because ") for reason in NOT_SHOWN.values())


def test_every_report_field_is_rendered_in_print_template_or_has_explicit_omission_reason() -> None:
    template = build_environment().get_template("report_print.html")
    report = full_report()
    baseline = template.render(report=report, print_view=True)
    covered: set[str] = set()
    failures = []
    for keys, changed in variations(report):

        class DisplayReport:
            contradictions = report.contradictions

            def __init__(self, data: Report) -> None:
                self.data = data

            def __getattr__(self, name: str) -> Any:
                return getattr(self.data, name)

        if template.render(report=DisplayReport(changed), print_view=True) == baseline:
            failures.append(keys)
        covered.update(keys)
    assert not failures, f"Fields lost during rendering: {failures}"
    assert schema_fields(Report) <= covered, f"Fixture must exercise: {schema_fields(Report) - covered}"

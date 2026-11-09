"""REPORT-STORYLINE: the code-built storyline and summary (no model prose, every sentence cited)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from incident_report.checklist import build_candidates
from incident_report.domain import Report, ReportView, Role
from incident_report.facts import (
    ChecklistParty,
    CitedFact,
    CollisionObservation,
    ObservationNote,
    OverviewFacts,
    PartyFact,
    StructuredFact,
)
from incident_report.storyline import build_storyline, build_summary

VIEW = "aa" * 32
OTHER_VIEW = "cc" * 32
A = "white Toyota sedan"
B = "silver Nissan SUV"


def cited(text: str, t: float, view: str = VIEW) -> CitedFact:
    return CitedFact(description=text, view_id=view, timestamp_seconds=t, confidence=0.9)


def contact(
    subject: str, other: str, t: float, kind: str = "vehicle_contact_vehicle", **extra: object
) -> CollisionObservation:
    return CollisionObservation.model_validate(
        {
            "kind": kind,
            "subject": subject,
            "other_party": other,
            "injury_grade": "unknown",
            "view_id": VIEW,
            "timestamp_seconds": t,
            "confidence": 0.9,
            "visibility": "visible",
            **extra,
        }
    )


def facts(
    events: Sequence[CitedFact] = (),
    contacts: Sequence[CollisionObservation] = (),
    candidates: Sequence[CitedFact] = (),
    notes: Sequence[ObservationNote] = (),
) -> OverviewFacts:
    return OverviewFacts(
        parties=[
            PartyFact(description=name, view_id=VIEW, timestamp_seconds=1.0, confidence=0.9)
            for name in (A, B, "the barrier")
        ],
        events=list(events),
        candidate_collisions=list(candidates) or build_candidates(list(contacts)),
        behaviour_observations=[],
        location_conditions=[],
        collision_observations=list(contacts),
        observation_notes=list(notes),
    )


def report(f: OverviewFacts | None, valid: bool = True) -> Report:
    view = ReportView(
        filename="a.mp4", role=Role.EXTERIOR, label="cam", acquisition_method="x", sha256=VIEW, duration_seconds=20.0
    )
    return Report(
        run_id=uuid.uuid4(),
        input_hash="bb" * 32,
        prompt_version="overview-11",
        provenance="gemini-api",
        summary="placeholder",
        views=(view,),
        facts=f,
        is_valid=valid,
    )


def test_events_and_contacts_merge_in_time_order_and_every_sentence_is_cited() -> None:
    f = facts(
        events=[cited(f"{B} brakes", 6.0), cited(f"{A} approaches the junction", 2.0)],
        contacts=[contact(A, B, 4.0)],
    )
    items = build_storyline(f)
    assert [i.timestamp_seconds for i in items] == [2.0, 4.0, 6.0]
    assert items[1].kind == "contact"
    assert A in items[1].text and B in items[1].text
    assert all(i.view_id == VIEW and i.text for i in items)


def test_an_event_that_repeats_a_contact_is_dropped_but_a_near_one_with_other_parties_stays() -> None:
    f = facts(
        events=[
            cited(f"{A} hits {B}", 4.1),  # same pair within 0.25 s: duplicate of the contact
            cited(f"{A} slows", 4.1),  # one party only: not the same pair
            cited(f"{A} hits {B}", 5.0),  # same pair but far in time
        ],
        contacts=[contact(A, B, 4.0)],
    )
    texts = [i.text for i in build_storyline(f)]
    assert f"{A} hits {B}" in texts  # the 5.0 s one
    assert sum(t == f"{A} hits {B}" for t in texts) == 1
    assert f"{A} slows" in texts
    assert len(texts) == 3


def test_contact_kinds_have_plain_sentences_and_obscured_touch_is_said() -> None:
    f = facts(
        contacts=[
            contact(A, B, 4.0, visibility="possible_obscured"),
            contact(A, "the barrier", 7.0, kind="contact_other"),
            contact(A, "", 8.0, kind="vehicle_overturned"),
            contact(B, "", 9.0, kind="vehicle_stopped_at_scene"),
        ]
    )
    texts = [i.text for i in build_storyline(f)]
    assert texts[0].endswith("(touch not clearly visible)")
    assert "the barrier" in texts[1]
    assert "overturn" in texts[2]
    assert "stops" in texts[3]


def test_old_report_without_typed_observations_uses_candidate_collisions() -> None:
    f = facts(events=[cited("old event", 1.0)], candidates=[cited(f"{A}: vehicle contact vehicle ({B})", 3.0)])
    items = build_storyline(f)
    assert [i.kind for i in items] == ["event", "contact"]


def test_views_keep_their_own_clock_in_view_order() -> None:
    f = facts(events=[cited("second view early", 1.0, OTHER_VIEW), cited("first view late", 9.0)])
    items = build_storyline(f, view_order=(VIEW, OTHER_VIEW))
    assert [i.text for i in items] == ["first view late", "second view early"]


def test_empty_storyline() -> None:
    assert build_storyline(None) == ()
    assert build_storyline(facts()) == ()


def test_summary_for_a_contact_names_the_first_contact_and_its_time() -> None:
    f = facts(contacts=[contact(B, A, 9.0), contact(A, B, 4.0)])
    r = report(f)
    assert r.incident_status == "Collision"
    s = build_summary(r)
    assert A in s and B in s and "4.0 s" in s and "9.0" not in s


def test_summary_without_contact_and_cannot_determine() -> None:
    clear = facts().model_copy(
        update={
            "parties": facts().parties[:1],
            "structured_facts": [
                StructuredFact(
                    subject=subject,
                    field=field,  # type: ignore[arg-type]
                    value=value,
                    other_party="",
                    view_id=VIEW,
                    timestamp_seconds=2.0,
                    confidence=0.9,
                )
                for subject, field, value in [
                    ("scene", "officer_directing_traffic", "no"),
                    (A, "party_type", "car"),
                    (A, "movement", "straight"),
                    (A, "approach_control_faced", "signal"),
                    (A, "signal_state_when_crossing_stop_line", "green"),
                ]
            ],
        }
    )
    r = report(clear)
    assert r.incident_status == "No Incident"
    assert build_summary(r) == "No collision found in the reviewed footage."
    assert build_summary(report(None)).startswith("Cannot Determine")
    assert build_summary(report(facts(), valid=False)).startswith("Cannot Determine")


def test_summary_with_only_a_candidate_collision_uses_its_time() -> None:
    f = facts(candidates=[cited("legacy contact", 3.5)])
    assert "3.5 s" in build_summary(report(f))


def note(subject: str, text: str, t: float, view: str = VIEW, code: str = "P1") -> ObservationNote:
    return ObservationNote(
        party_code="scene" if subject == "scene" else code,
        timestamp_seconds=t,
        note=text,
        view_id=view,
        subject=subject,
    )


def test_observation_notes_join_the_storyline_including_scene_notes() -> None:
    f = facts(
        events=[cited(f"{A} approaches", 2.0)],
        notes=[
            note("scene", "Two-lane road, dry, daylight", 0.5),
            note(B, f"{B} turns left into the side street", 3.0),
        ],
        contacts=[contact(A, B, 4.0)],
    )
    assert [i.kind for i in build_storyline(f)] == ["event", "contact"]  # the report leaves notes out
    items = build_storyline(f, include_notes=True)
    assert [(i.kind, i.timestamp_seconds) for i in items] == [
        ("note", 0.5),
        ("event", 2.0),
        ("note", 3.0),
        ("contact", 4.0),
    ]
    assert all(i.view_id == VIEW for i in items)


def test_a_note_that_restates_a_contact_or_an_event_is_dropped() -> None:
    f = facts(
        events=[cited(f"{A} slows", 2.0)],
        notes=[note(A, f"{A} hits {B}", 4.1), note(A, f"{A} slows", 2.1), note(A, f"{A} stops", 6.0)],
        contacts=[contact(A, B, 4.0)],
    )
    assert [i.text for i in build_storyline(f, include_notes=True)] == [
        f"{A} slows",
        f"{A} and {B} make contact",
        f"{A}: {A} stops",
    ]


def texts(*notes: ObservationNote) -> list[str]:
    return [i.text for i in build_storyline(facts(notes=notes), include_notes=True)]


def test_party_notes_are_prefixed_with_the_display_name_even_when_the_text_has_a_code() -> None:
    assert texts(note(A, "White sedan circulating", 1.0), note(A, "P1 turns left", 2.0)) == [
        f"{A}: White sedan circulating",
        f"{A}: P1 turns left",
    ]


def test_scene_notes_are_prefixed_scene() -> None:
    assert texts(note("scene", "Dry road, daylight", 0.0)) == ["Scene: Dry road, daylight"]


def test_camera_vehicle_notes_use_the_camera_label() -> None:
    cam = "camera vehicle (white sedan)"
    assert texts(note(cam, "Brakes hard", 1.0, code="CAM")) == [f"{cam}: Brakes hard"]


def test_the_checklist_display_name_wins_and_a_missing_name_falls_back_to_the_code() -> None:
    row = ChecklistParty.model_construct(party_code="P2", view_id=VIEW, display_name="P2 grey car")
    f = facts(notes=[note("P2", "Stops", 1.0, code="P2"), note("P9", "Waits", 2.0, code="P9")])
    f = f.model_copy(update={"party_checklist": [row]})
    assert [i.text for i in build_storyline(f, include_notes=True)] == ["P2 grey car: Stops", "P9: Waits"]

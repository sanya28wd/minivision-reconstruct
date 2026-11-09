"""Typed answer consistency at the public checker and rules seams."""

import pytest

from incident_report.facts import OverviewFacts, PartyFact, StructuredFact
from incident_report.rules.consistency import check_consistency

A = "white SUV"
B = "red sedan"


def fact(subject: str, field: str, value: str, other: str = "", **changes: object) -> StructuredFact:
    return StructuredFact.model_validate(
        dict(
            subject=subject,
            field=field,
            value=value,
            other_party=other,
            view_id="view",
            timestamp_seconds=1.0,
            confidence=0.9,
        )
        | changes
    )


def overview(*records: StructuredFact) -> OverviewFacts:
    return OverviewFacts(
        parties=[PartyFact(description=n, view_id="view", timestamp_seconds=0.0, confidence=0.9) for n in (A, B)],
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=list(records),
    )


@pytest.mark.parametrize("movement", ["entering_roundabout", "circulating_roundabout"])
@pytest.mark.parametrize(
    "state,bad", [("not_in_roundabout", True), ("unknown", False), ("entering_from_approach", False)]
)
def test_roundabout_movement_vs_entry_state(movement: str, state: str, bad: bool) -> None:
    records = (fact(A, "movement", movement), fact(A, "roundabout_entry_state", state))
    issues = check_consistency(overview(*records), {"view": 5.0})
    assert bool(issues) is bad
    if bad:
        assert issues[0].subject == A
        assert issues[0].fields == ("movement", "roundabout_entry_state")
        assert issues[0].cited_facts == tuple(r.citation() for r in records)
        assert issues[0].reason


@pytest.mark.parametrize(
    "field,value,other_field,other_value,code",
    [
        ("movement", "entering_from_side_road", "entry_origin", "public_road_same_road", "side_road_origin"),
        ("movement", "circulating_roundabout", "conflict_zone_type", "unsignalised_junction", "roundabout_zone"),
        ("roundabout_entry_state", "exiting", "conflict_zone_type", "mid_block", "roundabout_zone"),
        ("signal_state_when_crossing_stop_line", "red", "approach_control_faced", "none_confirmed", "signal_control"),
        ("approach_control_faced", "signal", "conflict_zone_type", "unsignalised_junction", "signal_zone"),
        ("approach_control_faced", "signal", "conflict_zone_type", "roundabout_unsignalised", "signal_zone"),
        ("party_type", "pedestrian", "movement", "left_turn", "pedestrian_movement"),
        ("party_type", "pedestrian", "movement", "entering_roundabout", "pedestrian_movement"),
        ("motion_state_before_contact", "parked_at_kerb", "movement", "right_turn", "stationary_turn"),
        ("motion_state_before_contact", "parked_at_kerb", "movement", "u_turn", "stationary_turn"),
    ],
)
def test_conflicting_fields_keep_evidence_and_unknown_does_not_conflict(
    field: str, value: str, other_field: str, other_value: str, code: str
) -> None:
    subject = "scene" if other_field == "conflict_zone_type" else A
    first = fact(A, field, value)
    second = fact(subject, other_field, other_value)
    issues = check_consistency(overview(first, second), {"view": 5.0})
    assert len(issues) == 1
    assert issues[0].code == code
    assert issues[0].subject == A
    assert set(issues[0].fields) == {field, other_field}
    assert issues[0].cited_facts == (first.citation(), second.citation())
    unknown = second.model_copy(update={"value": "unknown"})
    assert check_consistency(overview(first, unknown), {"view": 5.0}) == ()


@pytest.mark.parametrize(
    "records",
    [
        (fact(A, "movement", "circulating_roundabout"), fact("scene", "conflict_zone_type", "roundabout_signalised")),
        (fact(A, "party_type", "pedestrian"), fact(A, "movement", "crossing_on_foot")),
        (fact(A, "motion_state_before_contact", "moving"), fact(A, "movement", "left_turn")),
        (
            fact(A, "signal_state_when_crossing_stop_line", "not_applicable"),
            fact(A, "approach_control_faced", "none_confirmed"),
        ),
        (fact(A, "signal_state_when_crossing_stop_line", "not_visible"), fact(A, "approach_control_faced", "unknown")),
        (fact(A, "signal_state_when_crossing_stop_line", "green"), fact(A, "approach_control_faced", "signal")),
    ],
)
def test_compatible_known_answers_are_not_issues(records: tuple[StructuredFact, ...]) -> None:
    assert check_consistency(overview(*records), {"view": 5.0}) == ()


# Explicit impossible cases from the round-2 review; other positions may be real.
RELATIONS = [
    "B_from_A_left",
    "B_from_A_right",
    "B_ahead_oncoming",
    "B_ahead_same_direction",
    "B_behind",
    "B_alongside_adjacent_lane",
]
IMPOSSIBLE_PAIRS = {
    ("B_ahead_same_direction", "B_ahead_same_direction"),
    ("B_ahead_same_direction", "B_ahead_oncoming"),
    ("B_ahead_oncoming", "B_ahead_same_direction"),
}


@pytest.mark.parametrize("first", RELATIONS + ["unknown"])
@pytest.mark.parametrize("second", RELATIONS + ["unknown"])
def test_pair_relations_have_explicit_reciprocals(first: str, second: str) -> None:
    records = (fact(A, "relative_position_of_other", first, B), fact(B, "relative_position_of_other", second, A))
    issues = check_consistency(overview(*records), {"view": 5.0})
    bad = (first, second) in IMPOSSIBLE_PAIRS
    assert {i.subject for i in issues} == ({A, B} if bad else set())
    assert all(i.code == "pair_relations" and len(i.cited_facts) == 2 for i in issues)


def test_pair_relations_do_not_mix_separate_views_or_pairs() -> None:
    records = (
        fact(A, "relative_position_of_other", "B_from_A_left", B),
        fact(B, "relative_position_of_other", "B_from_A_left", A, view_id="other"),
    )
    assert check_consistency(overview(*records), {"view": 5.0, "other": 5.0}) == ()
    assert check_consistency(overview(records[0]), {"view": 5.0}) == ()


@pytest.mark.parametrize(
    "group",
    [
        "parties",
        "unmatched_dense_parties",
        "events",
        "candidate_collisions",
        "behaviour_observations",
        "location_conditions",
        "structured_facts",
        "text_readings",
        "collision_observations",
        "driver_observations",
        "drowsiness_observations",
    ],
)
@pytest.mark.parametrize("timestamp,bad", [(5.0, False), (5.01, True)])
def test_every_cited_collection_obeys_view_duration(group: str, timestamp: float, bad: bool) -> None:
    data = overview().model_dump()
    record: dict[str, object] = dict(
        description="observation", view_id="view", timestamp_seconds=timestamp, confidence=0.9
    )
    if group in {"parties", "unmatched_dense_parties"}:
        record["description"] = A
    elif group == "structured_facts":
        record = fact(A, "movement", "straight", timestamp_seconds=timestamp).model_dump()
    elif group == "text_readings":
        record = dict(
            kind="signboard", text="STOP", language="en", view_id="view", timestamp_seconds=timestamp, confidence=0.9
        )
    elif group == "collision_observations":
        record = dict(
            kind="vehicle_contact_vehicle",
            subject=A,
            other_party=B,
            injury_grade="unknown",
            view_id="view",
            timestamp_seconds=timestamp,
            confidence=0.9,
        )
    elif group == "driver_observations":
        record = dict(
            kind="phone_in_hand", subject=A, view_id="view", start_seconds=1.0, end_seconds=timestamp, confidence=0.9
        )
    elif group == "drowsiness_observations":
        record = dict(
            kind="eye_closure",
            subject=A,
            view_id="view",
            timestamp_seconds=1.0,
            start_seconds=0.0,
            end_seconds=timestamp,
            confidence=0.9,
        )
    data[group] = [record]
    issues = check_consistency(OverviewFacts.model_validate(data), {"view": 5.0})
    assert bool(issues) is bad
    assert all(i.code == "timestamp_outside_view" and i.cited_facts for i in issues)
    if bad and group == "collision_observations":
        assert {i.subject for i in issues} == {A, B}
    # No duration is not evidence that an otherwise valid time is outside it.
    assert check_consistency(OverviewFacts.model_validate(data), {}) == ()


@pytest.mark.parametrize("subject", [A, B, "scene"])
def test_engine_issues_override_only_named_parties_and_remove_findings(subject: str) -> None:
    from incident_report.rules.consistency import ConsistencyIssue
    from incident_report.rules.engine import evaluate_fault
    from incident_report.rules.models import FaultStatus
    from test_collision_pair_selection import collision, signal_facts

    facts = signal_facts((A, B), [collision(3.0)], [])
    before = evaluate_fault(facts)
    assert [p.status for p in before] == [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT]
    cited = facts.structured_facts[0].citation()
    issue = ConsistencyIssue(
        code="example",
        subject=subject,
        fields=("movement",),
        reason="Contradictory movement answers.",
        cited_facts=(cited,),
    )
    after = evaluate_fault(facts, consistency_issues=(issue,))
    for old, new in zip(before, after, strict=True):
        if subject in {"scene", old.party_description}:
            assert new.status == FaultStatus.CANNOT_DETERMINE
            assert issue.reason in new.reason
            assert cited in new.cited_facts
            assert new.findings == ()
            assert new.rule_id is new.article is new.legal_source is None
        else:
            assert new == old


@pytest.mark.parametrize("template", ["report.html", "report_print.html"])
def test_report_persists_and_escapes_consistency_issues(template: str) -> None:
    from incident_report.domain import Report
    from incident_report.rules.consistency import ConsistencyIssue
    from incident_report.web.templating import build_environment
    from test_invalid_report_rendering import make_report

    report = make_report(is_valid=True, summary="Synthetic report")
    cited = fact(A, "movement", "straight").citation()
    issue = ConsistencyIssue(
        code="example",
        subject="<img src=x>",
        fields=("movement",),
        reason="<script>bad()</script>",
        cited_facts=(cited,),
    )
    report = Report.model_validate(report.model_dump() | {"consistency_issues": (issue,)})
    assert Report.model_validate_json(report.model_dump_json()).consistency_issues == (issue,)
    html = build_environment().get_template(template).render(report=report)
    assert "Consistency checks" in html
    assert "&lt;img src=x&gt;" in html and "<img src=x>" not in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html and "<script>bad()</script>" not in html
    assert "1.0" in html and "view" in html
    old = report.model_dump(exclude={"consistency_issues"})
    assert Report.model_validate(old).consistency_issues == ()


def test_cross_view_alias_issue_blocks_the_party_the_rules_reconcile() -> None:
    from incident_report.rules.engine import evaluate_fault
    from incident_report.rules.models import FaultStatus
    from test_collision_pair_selection import collision, signal_facts

    facts = signal_facts((A, B), [collision(3.0)], [])
    alias = "white sport utility vehicle"
    parties = [
        facts.parties[0].model_copy(update={"color": "white", "body_type": "SUV"}),
        facts.parties[1],
        facts.parties[0].model_copy(
            update={"description": alias, "color": "white", "body_type": "SUV", "view_id": "other"}
        ),
    ]
    facts = facts.model_copy(
        update={
            "parties": parties,
            "structured_facts": [
                *facts.structured_facts,
                fact(alias, "party_type", "pedestrian", view_id="other"),
                fact(alias, "movement", "left_turn", view_id="other"),
            ],
        }
    )
    issues = check_consistency(facts, {"exterior": 5.0, "other": 5.0})
    guarded = evaluate_fault(facts, consistency_issues=issues)
    party = next(p for p in guarded if p.party_description == A)
    assert party.status == FaultStatus.CANNOT_DETERMINE
    assert "inconsistent typed answers" in party.reason
    assert any("pedestrian" in c.description for c in party.cited_facts)


@pytest.mark.parametrize("alias", [A, "white sport utility vehicle"])
def test_split_view_contradictory_fields_cannot_decide_fault(alias: str) -> None:
    from incident_report.rules.engine import evaluate_fault
    from incident_report.rules.models import FaultStatus
    from test_collision_pair_selection import collision, signal_facts

    facts = signal_facts((A, B), [collision(3.0)], [])
    source = facts.parties[0].model_copy(update={"color": "white", "body_type": "SUV"})
    other_view = source.model_copy(update={"description": alias, "view_id": "other"})
    facts = facts.model_copy(
        update={
            "parties": [source, facts.parties[1], other_view],
            "structured_facts": [
                *facts.structured_facts,
                fact(alias, "roundabout_entry_state", "already_inside_circulating", view_id="other"),
            ],
        }
    )
    issues = check_consistency(facts, {"exterior": 5.0, "other": 5.0})
    assert {i.subject for i in issues} == {A}
    assert {c.view_id for i in issues for c in i.cited_facts} == {"exterior", "other"}
    guarded = evaluate_fault(facts, consistency_issues=issues)
    assert next(p for p in guarded if p.party_description == A).status == FaultStatus.CANNOT_DETERMINE
    assert next(p for p in guarded if p.party_description == B).status == FaultStatus.NOT_AT_FAULT


@pytest.mark.parametrize("elapsed,bad", [(0.0, True), (1.0, True), (1.01, False), (4.0, False)])
def test_impossible_pair_requires_nearby_timestamps(elapsed: float, bad: bool) -> None:
    records = (
        fact(A, "relative_position_of_other", "B_ahead_same_direction", B),
        fact(B, "relative_position_of_other", "B_ahead_same_direction", A, timestamp_seconds=1.0 + elapsed),
    )
    issues = check_consistency(overview(*records), {"view": 10.0})
    assert bool(issues) is bad


@pytest.mark.parametrize(
    "first,second",
    [
        ("B_from_A_left", "B_from_A_left"),  # oncoming cars passing
        ("B_from_A_right", "B_from_A_right"),
        ("B_ahead_oncoming", "B_from_A_left"),  # turn across oncoming traffic
        ("B_from_A_right", "B_ahead_oncoming"),
        ("B_alongside_adjacent_lane", "B_ahead_same_direction"),  # offset sideswipe
        ("B_behind", "B_alongside_adjacent_lane"),
        ("B_behind", "B_behind"),  # back-to-back reversing
    ],
)
def test_real_pair_geometry_does_not_create_issues(first: str, second: str) -> None:
    assert (
        check_consistency(
            overview(fact(A, "relative_position_of_other", first, B), fact(B, "relative_position_of_other", second, A)),
            {"view": 5.0},
        )
        == ()
    )


@pytest.mark.parametrize("movement", ["left_turn", "right_turn", "u_turn"])
def test_stopped_party_waiting_to_turn_is_not_contradictory(movement: str) -> None:
    assert (
        check_consistency(
            overview(fact(A, "motion_state_before_contact", "stopped_in_lane"), fact(A, "movement", movement)),
            {"view": 5.0},
        )
        == ()
    )


@pytest.mark.parametrize(
    "movement",
    [
        "lane_change_left",
        "lane_change_right",
        "merging",
        "entering_from_side_road",
        "entering_from_car_park_or_driveway",
        "entering_roundabout",
    ],
)
def test_stopped_party_waiting_to_enter_merge_or_change_lane_is_not_contradictory(movement: str) -> None:
    issues = check_consistency(
        overview(fact(A, "motion_state_before_contact", "stopped_in_lane"), fact(A, "movement", movement)),
        {"view": 5.0},
    )
    assert issues == ()


@pytest.mark.parametrize("signal", ["red", "green", "signal_dark"])
def test_visible_signal_with_officer_control_is_not_contradictory(signal: str) -> None:
    assert (
        check_consistency(
            overview(
                fact(A, "signal_state_when_crossing_stop_line", signal), fact(A, "approach_control_faced", "officer")
            ),
            {"view": 5.0},
        )
        == ()
    )


@pytest.mark.parametrize("zone", ["signalised_junction", "roundabout_signalised"])
def test_signal_in_a_signalised_zone_is_not_contradictory(zone: str) -> None:
    assert (
        check_consistency(
            overview(fact(A, "approach_control_faced", "signal"), fact("scene", "conflict_zone_type", zone)),
            {"view": 5.0},
        )
        == ()
    )


@pytest.mark.parametrize("same_party", [False, True])
def test_shared_description_aliases_are_scoped_by_view(same_party: bool) -> None:
    from test_collision_pair_selection import collision, signal_facts

    facts = signal_facts((A, B), [collision(3.0)], [])
    first = facts.parties[0].model_copy(update={"color": "white", "body_type": "SUV"})
    second = facts.parties[1].model_copy(update={"color": "red", "body_type": "sedan"})
    parties = [
        first,
        second,
        first.model_copy(update={"description": "shared vehicle", "view_id": "one"}),
        second.model_copy(update={"description": "shared vehicle", "view_id": "two"}),
    ]
    records = [
        fact("shared vehicle", "party_type", "pedestrian", view_id="one"),
        fact("shared vehicle", "movement", "left_turn", view_id="one" if same_party else "two"),
    ]
    facts = facts.model_copy(update={"parties": parties, "structured_facts": records})
    issues = check_consistency(facts, {"one": 5.0, "two": 5.0, "exterior": 5.0})
    assert {i.subject for i in issues} == ({A} if same_party else set())


def test_distinct_typed_parties_with_identical_display_names_do_not_share_fields() -> None:
    parties = [
        PartyFact(
            description="shared vehicle",
            color=color,
            body_type=body,
            view_id=view,
            timestamp_seconds=0.0,
            confidence=0.9,
        )
        for color, body, view in [("white", "SUV", "one"), ("red", "sedan", "two")]
    ]
    records = [
        fact("shared vehicle", "party_type", "pedestrian", view_id="one"),
        fact("shared vehicle", "movement", "left_turn", view_id="two"),
    ]
    facts = OverviewFacts(
        parties=parties,
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=records,
    )
    assert check_consistency(facts, {"one": 5.0, "two": 5.0}) == ()


@pytest.mark.parametrize("template", ["report.html", "report_print.html"])
def test_overview_issues_are_escaped_cited_and_legacy_compatible(template: str) -> None:
    from incident_report.domain import Report
    from incident_report.rules.consistency import ConsistencyIssue
    from incident_report.web.templating import build_environment
    from test_invalid_report_rendering import make_report

    cited = fact(A, "movement", "straight").citation()
    issue = ConsistencyIssue(
        code='" onmouseover="bad()',
        subject="<img src=x>",
        fields=("movement",),
        reason="<script>bad()</script>",
        cited_facts=(cited,),
    )
    report = Report.model_validate(
        make_report(is_valid=True, summary="Synthetic report").model_dump()
        | {"overview_consistency_issues": ((), (issue,))}
    )
    assert Report.model_validate_json(report.model_dump_json()).overview_consistency_issues == ((), (issue,))
    html = build_environment().get_template(template).render(report=report)
    assert "Overview analysis 2" in html and "view" in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html and "<script>bad()</script>" not in html
    assert "&lt;img src=x&gt;" in html and "<img src=x>" not in html
    assert 'data-consistency-check="&#34; onmouseover=&#34;bad()"' in html
    assert (
        Report.model_validate(report.model_dump(exclude={"overview_consistency_issues"})).overview_consistency_issues
        == ()
    )

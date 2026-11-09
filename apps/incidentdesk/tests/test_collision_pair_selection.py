"""RPT-36 T2: choose the collision pair from typed contacts, conservatively (ADR 0006)."""

from incident_report.facts import CitedFact, CollisionObservation, OverviewFacts, PartyFact, StructuredFact
from incident_report.rules import FaultStatus, PartyFault, evaluate_fault

A = "white SUV"
B = "red sedan"
C = "yellow taxi"
D = "blue hatchback"
NOT_ASSESSED = "Not in the collision pair; not assessed"


def party(name: str) -> PartyFact:
    return PartyFact(description=name, view_id="exterior", timestamp_seconds=1.0, confidence=0.9)


def collision(t: float, view: str = "exterior") -> CitedFact:
    return CitedFact(description=f"contact at {t}", view_id=view, timestamp_seconds=t, confidence=0.9)


def contact(subject: str, other: str, t: float, kind: str = "vehicle_contact_vehicle") -> CollisionObservation:
    return CollisionObservation(
        kind=kind,  # type: ignore[arg-type]
        subject=subject,
        other_party=other,
        injury_grade="unknown",
        view_id="exterior",
        timestamp_seconds=t,
        confidence=0.9,
    )


def sf(subject: str, field: str, value: str, other: str = "") -> StructuredFact:
    return StructuredFact(
        subject=subject,
        field=field,  # type: ignore[arg-type]
        value=value,
        other_party=other,
        view_id="exterior",
        timestamp_seconds=2.0,
        confidence=0.9,
    )


def signal_facts(
    names: tuple[str, ...],
    collisions: list[CitedFact],
    observations: list[CollisionObservation],
) -> OverviewFacts:
    """A and B cross a signal (A red, B green); any other party is only listed."""
    structured = [
        sf("scene", "conflict_zone_type", "signalised_junction"),
        sf("scene", "officer_directing_traffic", "no"),
    ]
    for name, other, signal, relation in ((A, B, "red", "B_from_A_left"), (B, A, "green", "B_from_A_right")):
        structured += [
            sf(name, "party_type", "car"),
            sf(name, "movement", "straight"),
            sf(name, "approach_control_faced", "signal"),
            sf(name, "signal_state_when_crossing_stop_line", signal),
            sf(name, "relative_position_of_other", relation, other),
        ]
    return OverviewFacts(
        parties=[party(n) for n in names],
        events=[],
        candidate_collisions=collisions,
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=structured,
        collision_observations=observations,
    )


def by_name(faults: tuple[PartyFault, ...]) -> dict[str, PartyFault]:
    return {f.party_description: f for f in faults}


def test_bystander_third_party_pair_is_evaluated() -> None:
    faults = by_name(
        evaluate_fault(
            signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0)]), dense_confirmed_pairs={frozenset((A, B))}
        )
    )
    assert faults[A].status == FaultStatus.AT_FAULT
    assert faults[B].status == FaultStatus.NOT_AT_FAULT
    assert faults[C].status == FaultStatus.CANNOT_DETERMINE
    assert faults[C].reason.startswith(NOT_ASSESSED)
    assert faults[C].rule_id is None
    # Citations: pair observation present on the bystander, selection recorded on every party.
    assert faults[C].cited_facts
    for f in faults.values():
        assert f.pair_selection is not None
        assert {f.pair_selection.subject, f.pair_selection.other_party} == {A, B}
        assert f.pair_selection.observations
        assert f.pair_selection.collision == collision(3.0)
        assert f.pair_selection.reason


def test_car_car_contact_plus_pole_is_one_pair() -> None:
    pole = contact(B, "light pole", 6.0, kind="contact_other")
    faults = by_name(
        evaluate_fault(
            signal_facts((A, B), [collision(3.0), collision(6.0)], [contact(A, B, 3.0), pole]),
            dense_confirmed_pairs={frozenset((A, B))},
        )
    )
    assert faults[A].status == FaultStatus.AT_FAULT
    assert faults[B].status == FaultStatus.NOT_AT_FAULT
    selection = faults[A].pair_selection
    assert selection is not None and selection.collision == collision(3.0)


def test_nearest_candidate_chosen_by_time_in_same_view() -> None:
    faults = evaluate_fault(
        signal_facts((A, B), [collision(9.0), collision(3.2), collision(3.0, view="other")], [contact(A, B, 3.0)])
    )
    selection = faults[0].pair_selection
    assert selection is not None and selection.collision == collision(3.2)


def test_pole_hit_by_non_pair_party_is_not_selected() -> None:
    pole = contact(C, "light pole", 6.0, kind="contact_other")
    faults = evaluate_fault(signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0), pole]))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE and f.pair_selection is None for f in faults)
    assert all(f.reason.startswith("Multi-party collision: not covered") for f in faults)


def test_two_distinct_pairs_keep_structural_reason() -> None:
    faults = evaluate_fault(signal_facts((A, B, C, D), [collision(3.0)], [contact(A, B, 3.0), contact(C, D, 4.0)]))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE and f.pair_selection is None for f in faults)
    assert all(f.reason.startswith("Multi-party collision: not covered") for f in faults)


def test_two_pairs_two_collisions_keep_multi_collision_reason() -> None:
    faults = evaluate_fault(
        signal_facts((A, B, C), [collision(3.0), collision(5.0)], [contact(A, B, 3.0), contact(B, C, 5.0)])
    )
    assert all(f.pair_selection is None for f in faults)
    assert all(f.reason.startswith("Multi-collision incident: not covered") for f in faults)


def test_no_pair_observation_unchanged() -> None:
    faults = evaluate_fault(signal_facts((A, B, C), [collision(3.0)], []))
    assert all(f.pair_selection is None for f in faults)
    assert all(f.reason.startswith("Multi-party collision: not covered") for f in faults)


def test_pair_name_not_in_parties_unchanged() -> None:
    facts = signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0)])
    ghost = contact(A, B, 3.0).model_copy(update={"other_party": "green van"})
    faults = evaluate_fault(facts.model_copy(update={"collision_observations": [ghost]}))
    assert all(f.pair_selection is None for f in faults)
    assert all(f.reason.startswith("Multi-party collision: not covered") for f in faults)


def test_no_same_view_candidate_unchanged() -> None:
    faults = evaluate_fault(signal_facts((A, B, C), [collision(3.0, view="other")], [contact(A, B, 3.0)]))
    assert all(f.pair_selection is None for f in faults)


def test_equidistant_candidates_unchanged() -> None:
    faults = evaluate_fault(signal_facts((A, B), [collision(2.0), collision(4.0)], [contact(A, B, 3.0)]))
    assert all(f.pair_selection is None for f in faults)
    assert all(f.reason.startswith("Multi-collision incident: not covered") for f in faults)


def test_existing_two_party_one_collision_has_no_selection() -> None:
    faults = evaluate_fault(signal_facts((A, B), [collision(3.0)], [contact(A, B, 3.0)]))
    assert [f.status for f in faults] == [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT]
    assert all(f.pair_selection is None for f in faults)


def test_unselected_pair_with_unknown_facts_stays_cannot_determine() -> None:
    facts = signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0)])
    facts = facts.model_copy(update={"structured_facts": [s for s in facts.structured_facts if s.field != "movement"]})
    faults = by_name(evaluate_fault(facts))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults.values())
    assert faults[C].reason.startswith(NOT_ASSESSED)


def test_selected_pair_without_dense_confirmation_abstains() -> None:
    facts = signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0)])
    faults = by_name(evaluate_fault(facts))
    for name in (A, B):
        assert faults[name].status == FaultStatus.CANNOT_DETERMINE
        assert faults[name].reason == "Collision pair not confirmed by the close-up check"
        assert faults[name].rule_id is None
        assert faults[name].findings == ()
        assert contact(A, B, 3.0).citation() in faults[name].cited_facts
        selection = faults[name].pair_selection
        assert selection is not None
        assert selection.corroborated is False
    assert faults[C].reason.startswith(NOT_ASSESSED)


def test_matching_dense_pair_preserves_two_party_rule_result() -> None:
    facts = signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0)])
    faults = evaluate_fault(facts, dense_confirmed_pairs={frozenset((B, A))})
    ordinary = evaluate_fault(signal_facts((A, B), [collision(3.0)], [contact(A, B, 3.0)]))
    for actual, expected in zip(faults[:2], ordinary, strict=True):
        assert actual.model_copy(update={"pair_selection": None}) == expected
        assert actual.pair_selection is not None and actual.pair_selection.corroborated
    unconfirmed = evaluate_fault(facts)
    assert faults[2].model_copy(update={"pair_selection": None}) == unconfirmed[2].model_copy(
        update={"pair_selection": None}
    )


def test_different_dense_pair_and_one_different_name_do_not_confirm() -> None:
    facts = signal_facts((A, B, C, D), [collision(3.0)], [contact(A, B, 3.0)])
    expected = evaluate_fault(facts)
    for pair in ((C, D), (A, C), (A, B.upper())):
        assert evaluate_fault(facts, dense_confirmed_pairs={frozenset(pair)}) == expected


def test_no_selection_ignores_dense_confirmation_data() -> None:
    facts = signal_facts((A, B), [collision(3.0)], [contact(A, B, 3.0)])
    assert evaluate_fault(facts, dense_confirmed_pairs={frozenset((A, B))}) == evaluate_fault(facts)


def test_dense_pair_confirmation_uses_only_typed_pair_contacts() -> None:
    from incident_report.ai.dense import dense_confirmed_pairs

    empty = signal_facts((A, B, C), [collision(3.0)], [])
    pole = signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0, kind="contact_other")])
    matching = signal_facts((A, B, C), [], [contact(B, A, 4.0)])
    pedestrian = signal_facts((A, B, C, D), [], [contact(C, D, 5.0, kind="vehicle_struck_pedestrian")])
    assert dense_confirmed_pairs([]) == frozenset()
    assert dense_confirmed_pairs([empty, pole]) == frozenset()
    assert dense_confirmed_pairs([empty, pole, matching, pedestrian]) == {frozenset((A, B)), frozenset((C, D))}
    # A prose candidate or overview contact cannot stand in for a dense typed pair.
    assert all(
        f.status == FaultStatus.CANNOT_DETERMINE
        for f in evaluate_fault(
            signal_facts((A, B, C), [collision(3.0)], [contact(A, B, 3.0)]),
            dense_confirmed_pairs=dense_confirmed_pairs([empty, pole]),
        )
    )

"""RPT-27 synthetic fixtures; no live accuracy evidence."""

import json
from pathlib import Path
from typing import Any

import pytest

from incident_report.ai.gemini import parse_overview
from incident_report.facts import OverviewFacts
from incident_report.rules.collisions import classify_collisions
from test_collision_classification import SEDAN, SUV, VIEW, WALKER, collision, obs, payload


def facts6(observations: list[dict[str, Any]]) -> OverviewFacts:
    return parse_overview(
        json.dumps(payload([collision(2.0)], observations)), VIEW, prompt_version="overview-6", view_role="exterior"
    )


def test_simple_requires_unharmed_people_and_driven_away_vehicle() -> None:
    unharmed = obs("no_injury_visible", WALKER, 3.0)
    driven = obs("vehicle_driven_away", SEDAN, 4.0)
    assert classify_collisions(facts6([unharmed])).records[0].accident_outcome == "cannot_determine"
    assert classify_collisions(facts6([driven])).records[0].accident_outcome == "cannot_determine"
    record = classify_collisions(
        facts6(
            [
                obs("vehicle_struck_pedestrian", SEDAN, 2.0, other=WALKER),
                unharmed,
                obs("no_injury_visible", SEDAN, 3.0),
                driven,
            ]
        )
    ).records[0]
    assert record.accident_outcome == "simple"
    assert {c.timestamp_seconds for c in record.outcome_cited} == {3.0, 4.0}


def test_injury_and_scene_departure_produce_cited_article_38_without_fixed_fine() -> None:
    from incident_report.rules.referrals import evaluate_referrals

    facts = facts6(
        [
            obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV),
            obs("person_injured_visible", SUV, 3.0),
            obs("vehicle_left_scene", SEDAN, 4.0),
        ]
    )
    (referral,) = evaluate_referrals(facts, ())
    assert referral.article == "Art. 38"
    assert referral.party_description == SEDAN
    assert {c.timestamp_seconds for c in referral.cited_facts} == {2.0, 3.0, 4.0}
    assert referral.aed is referral.black_points is referral.impound_days is None
    assert referral.source_url == "https://uaelegislation.gov.ae/en/legislations/2598"


@pytest.mark.parametrize(
    "observations",
    [
        [obs("vehicle_left_scene", SEDAN, 4.0)],
        [obs("person_injured_visible", SUV, 3.0)],
        [obs("person_dead_visible", SUV, 3.0)],
        [
            obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV),
            obs("person_injured_visible", SUV, 3.0),
            obs("vehicle_left_scene", SEDAN, 1.0),
        ],
    ],
)
def test_missing_evidence_never_produces_referral(observations: list[dict[str, Any]]) -> None:
    from incident_report.rules.referrals import evaluate_referrals

    assert evaluate_referrals(facts6(observations), ()) == ()


def test_causing_death_requires_causal_fault_and_replaces_its_administrative_fine() -> None:
    from incident_report.facts import CollisionObservation
    from incident_report.web.templating import build_environment
    from test_no_incident_factors import A, B, facts, report_for

    observed = facts(collision=True)
    observed = observed.model_copy(
        update={
            "collision_observations": [
                CollisionObservation(**obs("vehicle_contact_vehicle", A, 3.0, other=B), view_id="road"),
                CollisionObservation(**obs("person_dead_visible", B, 4.0), view_id="road"),
            ]
        }
    )
    # Round 2 F1: supply the rules-stage decision; Report validation must never derive replacements.
    from incident_report.rules.referrals import replace_fines_with_referrals

    report = report_for(observed)
    report = report.model_copy(
        update={
            "violations": replace_fines_with_referrals(
                report.violations,
                report.referrals,
                report.parties,
            )
        }
    )
    (referral,) = report.referrals
    assert referral.article == "Art. 40" and referral.party_description == A
    assert report.violations == ()
    assert report.total_fines_aed == report.total_black_points == 0
    html = build_environment().get_template("report.html").render(report=report)
    section = html.split('data-referrals="criminal"')[1].split("</section>")[0]
    assert "Referral" in section and "Art. 40" in section and "@ 4.0s" in section
    assert "Federal Decree-Law No. 14 of 2024" in section
    assert "Fine (AED)" not in section and "50,000" not in section
    assert "33a" not in html
    assert report.__class__.model_validate_json(report.model_dump_json()).referrals == report.referrals


def jaywalking_facts() -> OverviewFacts:
    data = payload([collision(2.0)], [obs("vehicle_struck_pedestrian", SEDAN, 2.0, other=WALKER)])
    data["parties"] = data["parties"][::2]
    values = [
        ("scene", "officer_directing_traffic", "no", ""),
        ("scene", "conflict_zone_type", "mid_block", ""),
        (SEDAN, "party_type", "car", ""),
        (SEDAN, "movement", "straight", ""),
        (SEDAN, "approach_control_faced", "none_confirmed", ""),
        (WALKER, "party_type", "pedestrian", ""),
        (WALKER, "movement", "crossing_on_foot", ""),
        (WALKER, "pedestrian_crossing_place", "outside_any_crossing", ""),
        (WALKER, "relative_position_of_other", "B_from_A_left", SEDAN),
        (SEDAN, "relative_position_of_other", "B_from_A_right", WALKER),
    ]
    data["structured_facts"] = [
        dict(subject=s, field=f, value=v, other_party=o, timestamp_seconds=1.0, confidence=0.9) for s, f, v, o in values
    ]
    return parse_overview(json.dumps(data), VIEW, prompt_version="overview-6", view_role="exterior")


def test_jaywalking_causing_collision_gets_article_39_without_speed_threshold() -> None:
    from incident_report.rules.referrals import evaluate_referrals

    (referral,) = evaluate_referrals(jaywalking_facts(), ())
    assert referral.article == "Art. 39" and referral.party_description == WALKER
    assert any("outside_any_crossing" in c.description for c in referral.cited_facts)
    assert referral.aed is None


def test_legacy_hashes_and_default_unchanged() -> None:
    import hashlib
    from pathlib import Path

    import incident_report.facts as contracts
    from incident_report.ai.gemini import PROMPT_VERSION, GeminiSettings, overview_request_hash, prompt_for_version
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role

    baseline = json.loads(Path(__file__).with_name("referral_legacy_baseline.json").read_text())
    names = ["LEGACY_OVERVIEW_SCHEMA", "OVERVIEW_SCHEMA", "OVERVIEW_3_SCHEMA", "OVERVIEW_4_SCHEMA", "OVERVIEW_5_SCHEMA"]
    for i, name in enumerate(names, 1):
        version = f"overview-{i}"
        request = AIRequest(
            input_hash="a" * 64, prompt_version=version, views=(AIViewRef(Role.EXTERIOR, "b" * 64, 20.0),)
        )
        assert hashlib.sha256(prompt_for_version(version).encode()).hexdigest() == baseline[version]["prompt"]
        assert (
            hashlib.sha256(json.dumps(getattr(contracts, name), sort_keys=True).encode()).hexdigest()
            == baseline[version]["schema"]
        )
        assert overview_request_hash(request, GeminiSettings(model="gemini-2.5-pro")) == baseline[version]["request"]
    assert PROMPT_VERSION == "overview-2"
    assert "maxItems" not in json.dumps(contracts.OVERVIEW_6_SCHEMA)
    assert "vehicle_left_scene" in prompt_for_version("overview-6")


@pytest.mark.parametrize("kind", ["vehicle_left_scene", "vehicle_driven_away"])
def test_new_kinds_are_version_gated_and_require_citations(kind: str) -> None:
    from incident_report.ai.errors import AIConnectionError

    data = payload([collision(2.0)], [obs(kind, SEDAN, 3.0)])
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), VIEW, prompt_version="overview-5", view_role="exterior")
    del data["collision_observations"][0]["timestamp_seconds"]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), VIEW, prompt_version="overview-6", view_role="exterior")


@pytest.mark.parametrize(
    "field", ["collision_observations", "driver_observations", "structured_facts", "text_readings"]
)
def test_overview_6_requires_all_additive_fields(field: str) -> None:
    from incident_report.ai.errors import AIConnectionError

    data = payload()
    del data[field]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), VIEW, prompt_version="overview-6", view_role="exterior")


@pytest.mark.parametrize("field", ["relative_position_of_other", "movement", "pedestrian_crossing_place", "party_type"])
def test_jaywalking_missing_or_conflicting_evidence_abstains(field: str) -> None:
    from incident_report.rules.referrals import evaluate_referrals

    facts = jaywalking_facts()
    facts = facts.model_copy(update={"structured_facts": [f for f in facts.structured_facts if f.field != field]})
    assert evaluate_referrals(facts, ()) == ()


def test_referral_never_invents_fixed_penalties() -> None:
    from incident_report.rules.referrals import CriminalReferral

    facts = facts6([])
    for article in ("Art. 38", "Art. 39", "Art. 40"):
        with pytest.raises(ValueError):
            CriminalReferral.model_validate(
                dict(
                    party_description=SEDAN,
                    article=article,
                    reason="test",
                    cited_facts=tuple(facts.candidate_collisions),
                    source_url="https://uaelegislation.gov.ae/en/legislations/2598",
                    aed=500,
                )
            )


def test_cross_view_or_ambiguous_contact_evidence_abstains() -> None:
    from incident_report.rules.referrals import evaluate_referrals

    facts = facts6(
        [
            obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV),
            obs("person_injured_visible", SUV, 3.0),
            obs("vehicle_left_scene", SEDAN, 4.0),
        ]
    )
    other_view = facts.collision_observations[-1].model_copy(update={"view_id": "other"})
    assert (
        evaluate_referrals(
            facts.model_copy(update={"collision_observations": [*facts.collision_observations[:-1], other_view]}), ()
        )
        == ()
    )
    assert (
        evaluate_referrals(
            facts.model_copy(
                update={
                    "candidate_collisions": [
                        *facts.candidate_collisions,
                        facts.candidate_collisions[0].model_copy(update={"timestamp_seconds": 5.0}),
                    ]
                }
            ),
            (),
        )
        == ()
    )


def test_overview_6_transport_and_replay(tmp_path: "Path") -> None:
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo, prompt_for_version
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.ai.overview_replay import ReplayOverviewAIConnection
    from incident_report.domain import Role
    from incident_report.facts import OVERVIEW_6_SCHEMA
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_google_video_transport import FakeClient
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-6", (AIViewRef(Role.EXTERIOR, digest),))
    settings = GeminiSettings(model="gemini-3.8-flash")
    data = payload([collision(2.0)], [obs("vehicle_driven_away", SEDAN, 3.0)])
    response = GeminiAIConnection(
        settings, PublicResearchVideo(video, True), transport=FakeTransport([json.dumps(data)])
    ).analyse(req)
    assert response.facts is not None and response.facts.collision_observations
    assert ReplayOverviewAIConnection(settings, response.calls).analyse(req).facts == response.facts
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(video, True), prompt_for_version("overview-6"), settings)
    assert client.generated["config"]["response_schema"] == OVERVIEW_6_SCHEMA


def test_analysis_entry_retains_referral_with_zero_fixed_penalty(run_log: Any, tmp_path: "Path") -> None:
    from conftest import make_view
    from incident_report.ai.dense import DenseSettings
    from incident_report.domain import Origin, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from incident_report.web.templating import build_environment
    from test_new_incident_screen import SpyAIConnection
    from video_fixtures import synthetic_video

    video = synthetic_video(b"referral")
    digest = sha256_hex(video)
    data = payload(
        [collision(0.5)],
        [
            obs("vehicle_contact_vehicle", SEDAN, 0.5, other=SUV),
            obs("person_injured_visible", SUV, 0.6),
            obs("vehicle_left_scene", SEDAN, 1.0),
        ],
    )
    facts = parse_overview(json.dumps(data), digest, prompt_version="overview-6", view_role="exterior")
    report = analyse_incident(
        [make_view(video, Role.EXTERIOR)],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts={digest: facts}),
        run_log=run_log,
        prompt_version="overview-6",
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert report.is_valid
    (referral,) = report.referrals
    assert referral.article == "Art. 38" and referral.aed is None
    assert report.total_fines_aed == 0
    assert digest in build_environment().get_template("report.html").render(report=report)


def test_simple_ignores_driveability_before_contact() -> None:
    observations = [obs("vehicle_driven_away", SEDAN, 1.0), obs("no_injury_visible", WALKER, 3.0)]
    assert classify_collisions(facts6(observations)).records[0].accident_outcome == "cannot_determine"


def test_stopping_at_scene_prevents_article_38_even_if_vehicle_later_leaves() -> None:
    from incident_report.rules.referrals import evaluate_referrals

    facts = facts6(
        [
            obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV),
            obs("person_injured_visible", SUV, 3.0),
            obs("vehicle_stopped_at_scene", SEDAN, 2.5),
            obs("vehicle_left_scene", SEDAN, 4.0),
        ]
    )
    assert evaluate_referrals(facts, ()) == ()


def test_unknown_precontact_reading_does_not_backdate_known_crossing_place() -> None:
    from incident_report.rules.referrals import evaluate_referrals

    facts = jaywalking_facts()
    place = next(f for f in facts.structured_facts if f.field == "pedestrian_crossing_place")
    records = [f for f in facts.structured_facts if f is not place]
    records += [place.model_copy(update={"timestamp_seconds": 3.0}), place.model_copy(update={"value": "unknown"})]
    assert evaluate_referrals(facts.model_copy(update={"structured_facts": records}), ()) == ()


def test_one_vehicle_driveable_does_not_establish_simple_for_two_vehicles() -> None:
    observations = [
        obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV),
        obs("no_injury_visible", SEDAN, 3.0),
        obs("vehicle_driven_away", SEDAN, 4.0),
    ]
    assert classify_collisions(facts6(observations)).records[0].accident_outcome == "cannot_determine"


def test_legacy_criminal_row_without_referral_remains_court_decided() -> None:
    from incident_report.rules.decree_law_14_2024 import RULE_ART_7_4_PEDESTRIAN_SPEED
    from incident_report.rules.models import Violation
    from incident_report.rules.violations import map_violation
    from test_no_incident_factors import facts, report_for

    report = report_for(facts())
    row = map_violation(
        Violation(
            rule_id=RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id,
            party_description=SEDAN,
            cited_facts=(facts6([]).candidate_collisions[0],),
        )
    )
    data = report.model_dump()
    data["violations"] = (row,)
    reloaded = type(report).model_validate(data)
    # Owner-requested F2 reverses round 1's unconditional suppression expectation.
    assert reloaded.violations == (row,)
    assert reloaded.referrals == ()
    from incident_report.web.templating import build_environment

    assert "Decided by court" in build_environment().get_template("report.html").render(report=reloaded)

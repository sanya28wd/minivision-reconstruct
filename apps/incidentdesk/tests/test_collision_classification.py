"""RPT-25 # synthetic-fixture: every Collision with Dubai/MOI classification derived by code (ADR 0007).

The model reports only typed observations (what touched what, person visibly hurt, vehicle overturned).
Code derives Accident Type and Accident Outcome. None of this is accuracy evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import parse_overview
from incident_report.facts import OverviewFacts, validate_citations

REPO_ROOT = Path(__file__).resolve().parents[1]
SEDAN = "white Toyota sedan"
SUV = "black Nissan SUV"
WALKER = "pedestrian in a blue shirt"
VIEW = "v" * 64

# The official labels must never be requested from the model (ADR 0007): code derives them.
OFFICIAL_LABELS = ("Run-over", "Rollover", "Simple Accident", "Injury Accident", "Fatal Accident", "Accident Type")


def party(name: str) -> dict[str, Any]:
    return {"description": name, "timestamp_seconds": 0.0, "confidence": 0.9}


def collision(t: float, text: str = "contact between the vehicles") -> dict[str, Any]:
    return {"description": text, "timestamp_seconds": t, "confidence": 0.9}


def obs(kind: str, subject: str, t: float, *, other: str = "", grade: str = "unknown") -> dict[str, Any]:
    return {
        "kind": kind,
        "subject": subject,
        "other_party": other,
        "injury_grade": grade,
        "timestamp_seconds": t,
        "confidence": 0.9,
    }


def payload(collisions: Iterable[dict[str, Any]] = (), observations: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    return {
        "parties": [party(SEDAN), party(SUV), party(WALKER)],
        "events": [],
        "candidate_collisions": list(collisions),
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [],
        "driver_observations": [],
        "collision_observations": list(observations),
    }


def facts_for(
    collisions: Iterable[dict[str, Any]] = (), observations: Iterable[dict[str, Any]] = (), view: str = VIEW
) -> OverviewFacts:
    return parse_overview(
        json.dumps(payload(collisions, observations)), view, prompt_version="overview-5", view_role="exterior"
    )


# --- contract: parser, schema, fingerprints ---------------------------------------------------------


def test_overview_5_requires_the_flat_array_and_attaches_the_view() -> None:
    facts = facts_for([collision(2.0)], [obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV)])
    assert facts.collision_observations[0].view_id == VIEW
    assert facts.collision_observations[0].other_party == SUV
    missing = payload()
    del missing["collision_observations"]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(missing), VIEW, prompt_version="overview-5", view_role="exterior")


def test_earlier_versions_do_not_accept_collision_observations() -> None:
    data = payload([collision(2.0)], [obs("vehicle_overturned", SEDAN, 2.0)])
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), VIEW, prompt_version="overview-4", view_role="exterior")


@pytest.mark.parametrize(
    "bad",
    [
        obs("Run-over", SEDAN, 2.0),  # an official label, not an observation kind
        obs("severe_injury", WALKER, 2.0),  # severity is not an observation
        obs("person_injured_visible", WALKER, 2.0, grade="severe"),
        obs("vehicle_overturned", SEDAN, 2.0, grade="serious"),  # grade only on a visible injury
        obs("vehicle_contact_vehicle", SEDAN, 2.0),  # pair kind without the other party
        obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SEDAN),
        obs("vehicle_contact_vehicle", SEDAN, 2.0, other="red van"),  # not a described party
        obs("vehicle_overturned", SEDAN, 2.0, other=SUV),  # single-subject kind with an other party
        obs("vehicle_contact_vehicle", "Vehicle 1", 2.0, other=SUV),  # raw id
        obs("vehicle_overturned", "green bus", 2.0),  # not a described party
        obs("vehicle_overturned", SEDAN, -1.0),
        {**obs("vehicle_overturned", SEDAN, 2.0), "confidence": float("nan")},
        {**obs("vehicle_overturned", SEDAN, 2.0), "verdict": "Rollover"},
    ],
)
def test_parser_rejects_invalid_observations(bad: dict[str, Any]) -> None:
    with pytest.raises(AIConnectionError):
        parse_overview(
            json.dumps(payload([collision(2.0)], [bad])), VIEW, prompt_version="overview-5", view_role="exterior"
        )


def test_previous_contract_fingerprints_unchanged_and_prompt_is_versioned() -> None:
    from incident_report.ai.gemini import PROMPT_VERSION, GeminiSettings, overview_request_hash, prompt_for_version
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role
    from incident_report.facts import (
        LEGACY_OVERVIEW_SCHEMA,
        OVERVIEW_3_SCHEMA,
        OVERVIEW_4_SCHEMA,
        OVERVIEW_5_SCHEMA,
        OVERVIEW_SCHEMA,
    )

    baseline = json.loads(Path(__file__).with_name("collision_legacy_baseline.json").read_text())
    schemas = (LEGACY_OVERVIEW_SCHEMA, OVERVIEW_SCHEMA, OVERVIEW_3_SCHEMA, OVERVIEW_4_SCHEMA)
    for version, schema in zip(("overview-1", "overview-2", "overview-3", "overview-4"), schemas, strict=True):
        req = AIRequest(input_hash="a" * 64, prompt_version=version, views=(AIViewRef(Role.INTERIOR, "b" * 64, 20.0),))
        assert hashlib.sha256(prompt_for_version(version).encode()).hexdigest() == baseline[version]["prompt"]
        assert hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() == baseline[version]["schema"]
        assert overview_request_hash(req, GeminiSettings(model="gemini-2.5-pro")) == baseline[version]["request"]
    req5 = AIRequest(
        input_hash="a" * 64, prompt_version="overview-5", views=(AIViewRef(Role.EXTERIOR, "b" * 64, 20.0),)
    )
    assert overview_request_hash(req5, GeminiSettings(model="gemini-2.5-pro")) != baseline["overview-4"]["request"]
    assert PROMPT_VERSION == "overview-2", "the live default changes only after the coordinator's live smoke"
    assert "collision_observations" in OVERVIEW_5_SCHEMA["required"]
    assert "maxItems" not in json.dumps(OVERVIEW_5_SCHEMA)
    items = OVERVIEW_5_SCHEMA["properties"]["collision_observations"]["items"]
    assert items["type"] == "OBJECT" and all(v["type"] in {"STRING", "NUMBER"} for v in items["properties"].values())
    assert "view_id" not in items["properties"]


def test_prompt_asks_for_observations_never_for_official_labels() -> None:
    from incident_report.ai.gemini import prompt_for_version

    prompt = prompt_for_version("overview-5")
    assert prompt.startswith(prompt_for_version("overview-4"))
    assert "collision_observations" in prompt
    for label in OFFICIAL_LABELS:
        assert label not in prompt
    for kind in (
        "vehicle_contact_vehicle",
        "vehicle_struck_pedestrian",
        "vehicle_overturned",
        "person_injured_visible",
    ):
        assert kind in prompt


def test_transport_replay_and_dense_retention(tmp_path: Path) -> None:
    from incident_report.ai.dense import consolidate_facts
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo, prompt_for_version
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.ai.overview_replay import ReplayOverviewAIConnection
    from incident_report.domain import Role
    from incident_report.facts import OVERVIEW_5_SCHEMA
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_google_video_transport import FakeClient
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-5", (AIViewRef(Role.EXTERIOR, digest),))
    data = payload([collision(1.0)], [obs("vehicle_contact_vehicle", SEDAN, 1.0, other=SUV)])
    settings = GeminiSettings(model="gemini-3.8-flash")
    response = GeminiAIConnection(
        settings, PublicResearchVideo(video, True), transport=FakeTransport([json.dumps(data)])
    ).analyse(req)
    assert response.facts is not None and response.facts.collision_observations
    replayed = ReplayOverviewAIConnection(settings, response.calls).analyse(req)
    assert replayed.facts == response.facts
    assert consolidate_facts(response.facts, []).collision_observations == response.facts.collision_observations
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(video, True), prompt_for_version("overview-5"), settings)
    assert client.generated["config"]["response_schema"] == OVERVIEW_5_SCHEMA


# --- L0 citation validation -------------------------------------------------------------------------


def test_l0_requires_a_collision_on_the_same_view_for_each_observation() -> None:
    # Hand-built: parse_overview accepts it, L0 flags it (the observation points at no listed Collision).
    facts = facts_for([], [obs("vehicle_overturned", SEDAN, 2.0)])
    result = validate_citations(facts, [VIEW], view_durations={VIEW: 10.0})
    assert not result.is_valid
    assert any("collision_observation" in e and "no candidate collision" in e for e in result.errors)


def test_l0_checks_timestamp_range_and_confidence() -> None:
    facts = facts_for([collision(2.0)], [obs("vehicle_overturned", SEDAN, 20.0)])
    assert any("out-of-range" in e for e in validate_citations(facts, [VIEW], view_durations={VIEW: 10.0}).errors)
    low = payload([collision(2.0)], [{**obs("vehicle_overturned", SEDAN, 2.0), "confidence": 0.1}])
    facts = parse_overview(json.dumps(low), VIEW, prompt_version="overview-5", view_role="exterior")
    assert any("confidence" in e for e in validate_citations(facts, [VIEW], view_durations={VIEW: 10.0}).errors)


def test_l0_accepts_a_consistent_set() -> None:
    facts = facts_for([collision(2.0)], [obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV)])
    assert validate_citations(facts, [VIEW], view_durations={VIEW: 10.0}).is_valid


# --- deterministic classification (facts -> rules) --------------------------------------------------

CONTACT = obs("vehicle_contact_vehicle", SEDAN, 2.0, other=SUV)


def classify(collisions: Iterable[dict[str, Any]], observations: Iterable[dict[str, Any]], view: str = VIEW) -> Any:
    from incident_report.rules.collisions import classify_collisions

    return classify_collisions(facts_for(collisions, observations, view))


@pytest.mark.parametrize(
    "observation,expected",
    [
        (CONTACT, "collision_between_vehicles"),
        (obs("vehicle_struck_pedestrian", SEDAN, 2.0, other=WALKER), "run_over"),
        (obs("vehicle_overturned", SEDAN, 2.0), "rollover"),
        (obs("contact_other", SEDAN, 2.0, other="concrete barrier"), "other"),
    ],
)
def test_accident_type_is_derived_from_the_observed_form(observation: dict[str, Any], expected: str) -> None:
    record = classify([collision(2.0)], [observation]).records[0]
    assert record.accident_type == expected
    assert record.type_cited and record.type_cited[0].timestamp_seconds == 2.0


def test_no_form_observation_is_cannot_determine_never_a_guess() -> None:
    record = classify([collision(2.0)], []).records[0]
    assert record.accident_type == "cannot_determine"
    assert record.accident_outcome == "cannot_determine"
    assert record.injuries == ()
    assert record.type_reason and record.outcome_reason


def test_several_forms_on_one_collision_are_not_collapsed() -> None:
    record = classify([collision(2.0)], [CONTACT, obs("vehicle_overturned", SEDAN, 2.5)]).records[0]
    assert record.accident_type == "cannot_determine"
    assert "collision_between_vehicles" in record.type_reason and "rollover" in record.type_reason
    assert len(record.type_cited) == 2


def test_outcome_needs_visible_citations_and_never_comes_from_impact_severity() -> None:
    plain = classify([collision(2.0)], [CONTACT]).records[0]
    assert plain.accident_outcome == "cannot_determine"  # a contact alone proves nothing about injury
    # RPT-27 input extension: preserve the Simple assertion and supply both cited prerequisites.
    from incident_report.rules.collisions import classify_collisions

    simple = classify_collisions(
        parse_overview(
            json.dumps(
                payload(
                    [collision(2.0)],
                    [
                        CONTACT,
                        obs("no_injury_visible", WALKER, 3.0),
                        obs("no_injury_visible", SEDAN, 3.0),
                        obs("no_injury_visible", SUV, 3.0),
                        obs("vehicle_driven_away", SEDAN, 4.0),
                        obs("vehicle_driven_away", SUV, 4.0),
                    ],
                )
            ),
            VIEW,
            prompt_version="overview-6",
            view_role="exterior",
        )
    ).records[0]
    assert simple.accident_outcome == "simple" and simple.outcome_cited[0].timestamp_seconds == 3.0
    injured = classify([collision(2.0)], [CONTACT, obs("person_injured_visible", WALKER, 3.0)]).records[0]
    assert injured.accident_outcome == "injury" and injured.outcome_cited[0].timestamp_seconds == 3.0
    fatal = classify([collision(2.0)], [CONTACT, obs("person_dead_visible", WALKER, 4.0)]).records[0]
    assert fatal.accident_outcome == "fatal" and fatal.outcome_cited[0].timestamp_seconds == 4.0


def test_fatal_outranks_injury_outranks_simple_when_observations_overlap() -> None:
    both = [obs("no_injury_visible", SEDAN, 2.5), obs("person_injured_visible", WALKER, 3.0)]
    assert classify([collision(2.0)], [CONTACT, *both]).records[0].accident_outcome == "injury"
    allthree = [*both, obs("person_dead_visible", WALKER, 5.0)]
    assert classify([collision(2.0)], [CONTACT, *allthree]).records[0].accident_outcome == "fatal"


@pytest.mark.parametrize(
    "grade,expected",
    [("serious", "serious"), ("moderate", "moderate"), ("minor", "minor"), ("unknown", "cannot_determine")],
)
def test_injury_grade_only_when_the_observation_carries_it(grade: str, expected: str) -> None:
    record = classify([collision(2.0)], [CONTACT, obs("person_injured_visible", WALKER, 3.0, grade=grade)]).records[0]
    assert record.injuries[0].grade == expected
    assert record.injuries[0].subject == WALKER
    assert record.injuries[0].cited.timestamp_seconds == 3.0 and record.injuries[0].cited.view_id == VIEW


def test_no_injury_without_a_citation_ever() -> None:
    # The only way to an Injury outcome or an Injury Grade is a typed observation with view + timestamp.
    for record in classify([collision(2.0), collision(6.0)], [CONTACT]).records:
        assert record.injuries == ()
        assert record.accident_outcome == "cannot_determine"
    from incident_report.rules.collisions import CollisionRecord

    with pytest.raises(ValueError):  # an injury grade cannot exist without its citation
        CollisionRecord.model_validate(
            {**classify([collision(2.0)], []).records[0].model_dump(), "injuries": [{"subject": "x", "grade": "minor"}]}
        )


def test_cause_is_cannot_determine_because_the_repo_has_no_sourced_moi_list() -> None:
    record = classify([collision(2.0)], [CONTACT, obs("person_dead_visible", WALKER, 3.0)]).records[0]
    assert record.cause == "cannot_determine"
    assert "no sourced MOI cause list" in record.cause_reason


def test_every_collision_is_listed_in_order_including_minor_secondary_ones() -> None:
    records = classify(
        [collision(9.0, "tap against a kerb"), collision(2.0, "main impact"), collision(5.5, "secondary rear contact")],
        [
            obs("vehicle_overturned", SEDAN, 9.2),
            CONTACT,
            obs("person_injured_visible", WALKER, 3.0, grade="minor"),
            obs("contact_other", SUV, 5.5, other="road sign"),
        ],
    ).records
    assert [r.number for r in records] == [1, 2, 3]
    assert [r.collision.timestamp_seconds for r in records] == [2.0, 5.5, 9.0]
    assert [r.accident_type for r in records] == ["collision_between_vehicles", "other", "rollover"]
    assert [r.accident_outcome for r in records] == ["injury", "cannot_determine", "cannot_determine"]
    assert all(r.collision.view_id == VIEW for r in records)


def test_collisions_from_several_views_are_grouped_per_view_clock() -> None:
    from incident_report.rules.collisions import classify_collisions

    a, b = "a" * 64, "b" * 64
    fa = facts_for([collision(8.0)], [CONTACT.copy() | {"timestamp_seconds": 8.0}], view=a)
    fb = facts_for([collision(1.0)], [obs("vehicle_overturned", SUV, 1.0)], view=b)
    merged = OverviewFacts(
        parties=[*fa.parties, *fb.parties],
        events=[],
        candidate_collisions=[*fa.candidate_collisions, *fb.candidate_collisions],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=[],
        text_readings=[],
        driver_observations=[],
        collision_observations=[*fa.collision_observations, *fb.collision_observations],
    )
    records = classify_collisions(merged).records
    assert [(r.collision.view_id, r.collision.timestamp_seconds) for r in records] == [(a, 8.0), (b, 1.0)]
    assert [r.accident_type for r in records] == ["collision_between_vehicles", "rollover"]


def test_observations_attach_to_the_nearest_collision_on_the_same_view_only() -> None:
    a, b = "a" * 64, "b" * 64
    fa = facts_for([collision(2.0)], [], view=a)
    fb = facts_for([collision(2.0)], [obs("vehicle_overturned", SEDAN, 2.0)], view=b)
    merged = OverviewFacts(
        parties=[*fa.parties],
        events=[],
        candidate_collisions=[*fa.candidate_collisions, *fb.candidate_collisions],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=[],
        text_readings=[],
        driver_observations=[],
        collision_observations=[*fb.collision_observations],
    )
    from incident_report.rules.collisions import classify_collisions

    records = classify_collisions(merged).records
    assert records[0].accident_type == "cannot_determine"  # view a has no observation
    assert records[1].accident_type == "rollover"


def test_an_observation_on_a_view_with_no_collision_is_surfaced_not_dropped() -> None:
    summary = classify([], [obs("person_dead_visible", WALKER, 3.0)])
    assert summary.records == ()
    assert [c.timestamp_seconds for c in summary.unattached] == [3.0]


def test_legacy_facts_still_list_collisions_with_cannot_determine() -> None:
    from incident_report.rules.collisions import classify_collisions

    legacy = OverviewFacts.model_validate(
        {
            "parties": [],
            "events": [],
            "candidate_collisions": [
                {"description": "contact", "view_id": VIEW, "timestamp_seconds": 1.0, "confidence": 0.9}
            ],
        }
    )
    record = classify_collisions(legacy).records[0]
    assert record.accident_type == record.accident_outcome == record.cause == "cannot_determine"


def test_allowed_values_are_exactly_the_glossary_terms_and_every_one_has_a_label() -> None:
    from typing import get_args

    from incident_report.rules.collisions import AccidentOutcome, AccidentType, InjuryGrade
    from incident_report.web.labels import LabelCatalog

    glossary = (REPO_ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
    catalog = LabelCatalog.default()
    expected = {
        "accident_type": {"collision_between_vehicles", "run_over", "rollover", "other", "cannot_determine"},
        "accident_outcome": {"simple", "injury", "fatal", "cannot_determine"},
        "injury_grade": {"serious", "moderate", "minor", "cannot_determine"},
    }
    assert set(get_args(AccidentType)) == expected["accident_type"]
    assert set(get_args(AccidentOutcome)) == expected["accident_outcome"]
    assert set(get_args(InjuryGrade)) == expected["injury_grade"]
    for group, values in expected.items():
        for value in values - {"cannot_determine"}:
            label = catalog.get(f"{group}.{value}")
            assert label.en in glossary


# --- the report seam: analyse_incident + page -------------------------------------------------------


def _report_with(
    run_log: Any, tmp_path: Path, per_view: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]
) -> Any:
    from conftest import make_view
    from incident_report.ai.dense import DenseSettings
    from incident_report.domain import Origin, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_new_incident_screen import SpyAIConnection
    from video_fixtures import synthetic_video

    videos = [synthetic_video(f"collision-{i}".encode()) for i in range(len(per_view))]
    hashes = [sha256_hex(v) for v in videos]
    facts = {
        digest: parse_overview(
            json.dumps(payload(cols, observations)), digest, prompt_version="overview-5", view_role="exterior"
        )
        for digest, (cols, observations) in zip(hashes, per_view, strict=True)
    }
    return analyse_incident(
        [make_view(v, Role.EXTERIOR) for v in videos],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts=facts),
        run_log=run_log,
        prompt_version="overview-5",
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )


class _Collisions(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.cards: list[dict[str, str | None]] = []
        self.text: list[str] = []
        self.inside = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("data-report-section") == "collisions":
            self.inside = True
        if self.inside and "data-collision" in a:
            self.cards.append(a)

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "section":
            self.inside = False


def test_report_lists_every_collision_across_views_with_citations(run_log: Any, tmp_path: Path) -> None:
    report = _report_with(
        run_log,
        tmp_path,
        [
            (
                [collision(0.5, "main impact"), collision(1.0, "secondary tap")],
                [
                    obs("vehicle_contact_vehicle", SEDAN, 0.5, other=SUV),
                    obs("person_injured_visible", WALKER, 0.6, grade="moderate"),
                    obs("contact_other", SUV, 1.0, other="kerb"),
                ],
            ),
            ([collision(0.5, "other camera sees a rollover")], [obs("vehicle_overturned", SUV, 0.5)]),
        ],
    )
    assert report.is_valid
    records = report.collision_summary.records
    assert [r.number for r in records] == [1, 2, 3]
    assert [r.accident_type for r in records] == ["collision_between_vehicles", "other", "rollover"]
    assert [r.accident_outcome for r in records] == ["injury", "cannot_determine", "cannot_determine"]
    assert records[0].injuries[0].grade == "moderate"
    assert records[2].collision.view_id != records[0].collision.view_id
    assert {o.view_id for o in report.facts.collision_observations} == {r.collision.view_id for r in records}
    # Persisted and reloaded: nothing derived is stored, facts round-trip.
    assert report_roundtrip(report).collision_summary == report.collision_summary


def report_roundtrip(report: Any) -> Any:
    from incident_report.domain import Report

    return Report.model_validate_json(report.model_dump_json())


def test_page_shows_official_terms_in_both_languages_with_cannot_determine(run_log: Any, tmp_path: Path) -> None:
    from incident_report.web.templating import build_environment

    report = _report_with(
        run_log,
        tmp_path,
        [
            (
                [collision(0.5, "<b>main</b> impact"), collision(1.0, "secondary tap")],
                [
                    obs("vehicle_struck_pedestrian", SEDAN, 0.5, other=WALKER),
                    obs("person_injured_visible", WALKER, 0.6, grade="serious"),
                    obs("vehicle_overturned", SUV, 1.0),
                ],
            )
        ],
    )
    html = build_environment().get_template("report.html").render(report=report)
    page = _Collisions(html)
    assert [c["data-accident-type"] for c in page.cards] == ["run_over", "rollover"]
    assert [c["data-accident-outcome"] for c in page.cards] == ["injury", "cannot_determine"]
    text = " ".join("".join(page.text).split())
    for needle in (
        "Run-over",
        "دهس",
        "Rollover",
        "تدهور",
        "Injury Accident",
        "Serious",
        "بالغة",
        "Cause",
        "Cannot Determine",
    ):
        assert needle in text, needle
    assert "<b>main</b>" not in html and "&lt;b&gt;main&lt;/b&gt;" in html  # model text is escaped
    assert "@ 0.5s" in text and "@ 0.6s" in text


def test_page_never_shows_an_unlisted_label_or_invented_scale(run_log: Any, tmp_path: Path) -> None:
    from incident_report.web.templating import build_environment

    report = _report_with(
        run_log, tmp_path, [([collision(0.5)], [obs("vehicle_contact_vehicle", SEDAN, 0.5, other=SUV)])]
    )
    html = build_environment().get_template("report.html").render(report=report)
    section = html.split('data-report-section="collisions"')[1].split("</section>")[0]
    for banned in ("rear-end", "T-bone", "sideswipe", "low severity", "medium", "high severity", "Severity"):
        assert banned.casefold() not in section.casefold()

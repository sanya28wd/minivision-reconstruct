"""RPT-27 review regressions; synthetic facts, no live model calls."""

from html import unescape
from pathlib import Path

import pytest

from incident_report.domain import Report
from incident_report.rules.decree_law_14_2024 import RULE_ART_7_4_PEDESTRIAN_SPEED
from incident_report.rules.models import Violation
from incident_report.rules.violations import map_violation
from incident_report.runlog import RunLog
from incident_report.web.templating import build_environment
from test_collision_classification import SEDAN
from test_no_incident_factors import facts, report_for
from test_referrals import facts6, jaywalking_facts


def test_criminal_row_without_referral_survives_validation_reload_and_rendering() -> None:
    original = report_for(facts())
    row = map_violation(
        Violation(
            rule_id=RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id,
            party_description=SEDAN,
            cited_facts=(facts6([]).candidate_collisions[0],),
        )
    )
    data = original.model_dump()
    data["violations"] = (row,)
    constructed = Report.model_validate(data)
    assert constructed.referrals == ()
    assert constructed.violations == (row,)
    reloaded = Report.model_validate_json(constructed.model_dump_json())
    assert reloaded.violations == (row,)
    html = build_environment().get_template("report.html").render(report=reloaded)
    assert row.name_en in unescape(html)
    assert "Decided by court" in html


def test_matching_referral_does_not_rewrite_a_saved_report_on_reload() -> None:
    from test_collision_classification import WALKER

    original = report_for(jaywalking_facts())
    assert original.facts is not None
    row = map_violation(
        Violation(
            rule_id=RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id,
            party_description=WALKER,
            cited_facts=(original.facts.candidate_collisions[0],),
        )
    )
    saved = original.model_copy(update={"violations": (row,)})
    reloaded = Report.model_validate_json(saved.model_dump_json())
    assert reloaded.referrals
    assert reloaded.violations == saved.violations
    assert reloaded.model_dump_json() == saved.model_dump_json()


def test_replacement_requires_same_party_article_and_catalogue_row() -> None:
    from incident_report.rules.referrals import evaluate_referrals, replace_fines_with_referrals
    from test_collision_classification import WALKER

    observed = jaywalking_facts()
    (referral,) = evaluate_referrals(observed, ())
    citation = observed.candidate_collisions[0]
    rows = tuple(
        map_violation(
            Violation(rule_id="fixture", party_description=party, cited_facts=(citation,)),
            rule_mapping={"fixture": key},
        )
        for party, key in [
            (WALKER, "referral_art_39"),
            (SEDAN, "referral_art_39"),
            (WALKER, "referral_art_38"),
            (WALKER, "red_light_light"),
        ]
    )
    assert replace_fines_with_referrals(rows, (), ()) == rows
    assert replace_fines_with_referrals(rows, (referral,), ()) == rows[1:]
    assert rows[0].aed is rows[1].aed is rows[2].aed is None


def test_replacement_uses_referral_catalogue_item_number() -> None:
    from dataclasses import replace

    from incident_report.fines import FinesTable
    from incident_report.rules.referrals import evaluate_referrals, replace_fines_with_referrals
    from test_collision_classification import WALKER

    observed = jaywalking_facts()
    table = FinesTable.default()
    # Synthetic alternate catalogue label proves that rule code does not hard-code item 39.
    table = replace(
        table,
        referrals=tuple(replace(r, item_number="39a") if r.key == "referral_art_39" else r for r in table.referrals),
    )
    row = map_violation(
        Violation(rule_id="fixture", party_description=WALKER, cited_facts=(observed.candidate_collisions[0],)),
        fines_table=table,
        rule_mapping={"fixture": "referral_art_39"},
    )
    assert row.item_number == "39a"
    assert replace_fines_with_referrals((row,), evaluate_referrals(observed, ()), (), fines_table=table) == ()


@pytest.mark.parametrize("offset,expected", [(-0.009, True), (0.009, True), (-0.011, False), (0.011, False)])
def test_contact_matching_uses_same_view_tolerance(offset: float, expected: bool) -> None:
    from incident_report.rules.referrals import evaluate_referrals
    from test_collision_classification import SUV, obs

    observed = facts6(
        [
            obs("vehicle_contact_vehicle", SEDAN, 2.0 + offset, other=SUV),
            obs("person_injured_visible", SUV, 3.0),
            obs("vehicle_left_scene", SEDAN, 4.0),
        ]
    )
    assert bool(evaluate_referrals(observed, ())) is expected
    changed_view = observed.collision_observations[0].model_copy(update={"view_id": "other-view"})
    observed = observed.model_copy(
        update={"collision_observations": [changed_view, *observed.collision_observations[1:]]}
    )
    assert evaluate_referrals(observed, ()) == ()


def test_rules_stage_persists_fatal_referral_replacement(run_log: "RunLog", tmp_path: "Path") -> None:
    from conftest import make_view
    from incident_report.ai.dense import DenseSettings
    from incident_report.domain import Origin, Role
    from incident_report.entry import analyse_incident
    from incident_report.facts import CollisionObservation, OverviewFacts
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_collision_classification import obs
    from test_new_incident_screen import SpyAIConnection
    from test_no_incident_factors import A, B
    from video_fixtures import synthetic_video

    video = synthetic_video(b"referral-round2")
    digest = sha256_hex(video)
    data = facts(collision=True).model_dump()
    data["collision_observations"] = [
        CollisionObservation(**obs("vehicle_contact_vehicle", A, 3.0, other=B), view_id="road").model_dump(),
        CollisionObservation(**obs("person_dead_visible", B, 4.0), view_id="road").model_dump(),
    ]
    # Retain the causal scenario inside the short synthetic video's duration.
    for group in data.values():
        for item in group:
            item["view_id"] = digest
            item["timestamp_seconds"] /= 4
    observed = OverviewFacts.model_validate(data)
    report = analyse_incident(
        [make_view(video, Role.EXTERIOR)],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts={digest: observed}),
        run_log=run_log,
        prompt_version="overview-6",
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert report.is_valid
    assert [r.article for r in report.referrals] == ["Art. 40"]
    assert report.violations == ()
    assert report.total_fines_aed == report.total_black_points == 0
    assert Report.model_validate_json(report.model_dump_json()).violations == ()
    saved = run_log.get(report.run_id).report
    assert saved is not None
    assert saved.violations == report.violations
    assert saved.referrals == report.referrals

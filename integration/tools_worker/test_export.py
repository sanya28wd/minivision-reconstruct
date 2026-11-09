"""Reviewed bundle export preserves producer bytes and pins its own envelope."""

import json
import pytest

from integration.tools_worker.test_replay import replay_cases
from integration.tools_worker.test_worker import ROOT, request


def test_export_reviewed_bundle_under_configured_root(tmp_path):
    from integration.tools_worker.app import create_app
    from minivision_reconstruct.incident.review import review_revisions
    from minivision_reconstruct.evidence.integrity import sha256_file

    cases = replay_cases(tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    incident = cases / "mig02-multi-review/analysis/incident/mig02-incident"
    revision = review_revisions(incident)[-1]
    template = json.loads(
        (
            ROOT / "contracts/evidence-v1/fixtures/mig02-multi-review/bundle.json"
        ).read_bytes()
    )
    unrelated = cases / "mig02-multi-review/analysis/tracks/unrelated/history.json"
    unrelated.parent.mkdir()
    unrelated.write_text('{"schema_version":"1.0","not_selected":true}')
    app = create_app(cases, "test-local-token", exports_root=exports)
    response = request(
        app,
        "/cases/mig02-multi-review/replay/week8-incident",
        data={
            "action": "export_attach",
            "action_id": "00000000-0000-0000-0000-000000000001",
            "reviewer": "Reviewer",
            "reason": "Attach reviewed evidence",
            "scene_revision": "initial",
            "marker_revision": "initial",
            "incident_run": "mig02-incident",
            "review_revision": revision,
            "export_template": template,
            "source_view_id": template["sources"][0]["view_id"],
        },
    )
    assert response.status_code == 200, response.text
    destination = exports / response.json()["bundle"]
    envelope = json.loads((destination / "bundle.json").read_bytes())
    assert envelope["sources"][0]["producer_case_id"] == "mig02-multi-review"
    assert envelope["sources"][0]["review_revision"] == revision
    assert (
        "mig02-multi-review/analysis/tracks/unrelated/history.json"
        not in envelope["artifacts"]
    )
    assert (
        envelope["sources"][0]["reviewed_manifest"]
        == f"mig02-multi-review/analysis/incident/mig02-incident/reviewed/{revision}/manifest.json"
    )
    for name, digest in envelope["artifacts"].items():
        assert (destination / name).read_bytes() == (cases / name).read_bytes()
        assert sha256_file(destination / name) == digest


def test_cases_and_exports_roots_cannot_overlap(tmp_path):
    from integration.tools_worker.app import create_app

    cases = tmp_path / "cases"
    cases.mkdir()
    nested = cases / "bundles"
    nested.mkdir()
    for exports in (nested, cases, tmp_path):
        with pytest.raises(ValueError):
            create_app(cases, "test-local-token", exports_root=exports)

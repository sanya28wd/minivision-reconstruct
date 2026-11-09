"""Workspace attachment binds local reconstruction proposals to existing incident videos."""

import json
import uuid
from pathlib import Path

import psycopg
import pytest

from evidence_import_fixtures import FIXTURES
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.reconstruction.store import Workspace, WorkspaceRefused


@pytest.fixture
def incident(database_url: str) -> uuid.UUID:
    identifier = uuid.uuid4()
    envelope = json.loads((FIXTURES / "mig02-relative" / "bundle.json").read_bytes())
    view = envelope["sources"][0]["original_video_sha256"]
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,origin,created_at,input_hash,report,status,total_latency_ms) VALUES (%s,'app',now(),%s,%s::jsonb,'succeeded',0)",
            (identifier, "a" * 64, json.dumps({"views": [{"sha256": view}]})),
        )
    return identifier


def test_attach_is_hash_bound_and_idempotent(database_url: str, incident: uuid.UUID) -> None:
    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, FIXTURES))
        first = workspace.attach(incident, "mig02-relative", "synthetic reviewer")
        second = workspace.attach(incident, "mig02-relative", "synthetic reviewer")
        assert first == second and len(workspace.current(incident)) == 1
        with pytest.raises(WorkspaceRefused, match="different video"):
            workspace.attach(incident, "mig02-001-01-rear-end", "reviewer")


@pytest.mark.parametrize("relative", ["../elsewhere", "/absolute", "C:/outside", "a\\b"])
def test_paths_refuse_before_import(database_url: str, incident: uuid.UUID, relative: str) -> None:
    with psycopg.connect(database_url) as conn, pytest.raises((ValueError, WorkspaceRefused)):
        Workspace(conn, ReconstructionSettings(True, FIXTURES)).attach(incident, relative, "reviewer")


def test_feature_off_refuses_attachment(database_url: str, incident: uuid.UUID) -> None:
    with psycopg.connect(database_url) as conn, pytest.raises(WorkspaceRefused, match="disabled"):
        Workspace(conn, ReconstructionSettings(False, FIXTURES)).attach(incident, "mig02-relative", "reviewer")


def test_mapping_requires_same_view_and_keeps_action_history(database_url: str, incident: uuid.UUID) -> None:
    from incident_report.reconstruction.mapping import map_track, mapping_history, track_options

    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, FIXTURES))
        link = workspace.attach(incident, "mig02-relative", "reviewer")[0]
        options = track_options(workspace, incident, link)
        assert options and all(option.action == "unmapped" for option in options)
        conn.execute(
            "UPDATE runs SET report=jsonb_set(report,'{facts}',%s::jsonb) WHERE run_id=%s",
            (
                json.dumps(
                    {
                        "parties": [
                            {"view_id": link.view_sha256, "party_code": "P1", "description": "White Toyota sedan"},
                            {"view_id": "b" * 64, "party_code": "P2", "description": "Blue Ford pickup"},
                        ]
                    }
                ),
                incident,
            ),
        )
        ref = options[0].track_ref
        with pytest.raises(ValueError, match="same view"):
            map_track(workspace, incident, link.import_id, link.view_sha256, ref, "map", "P2", "reviewer", "checked")
        map_track(workspace, incident, link.import_id, link.view_sha256, ref, "map", "P1", "reviewer", "checked")
        assert track_options(workspace, incident, link)[0].label == "White Toyota sedan"
        map_track(workspace, incident, link.import_id, link.view_sha256, ref, "unmap", None, "reviewer", "uncertain")
        map_track(
            workspace, incident, link.import_id, link.view_sha256, ref, "cannot_determine", None, "reviewer", "occluded"
        )
        assert [row.action for row in mapping_history(workspace, incident)] == ["map", "unmap", "cannot_determine"]
        assert track_options(workspace, incident, link)[0].action == "cannot_determine"


def test_superseded_mapping_requires_explicit_reconfirmation(
    database_url: str, incident: uuid.UUID, tmp_path: Path
) -> None:
    import shutil

    from incident_report.reconstruction.mapping import map_track, mapping_history, track_options

    root = tmp_path / "exports"
    for name in ("one", "two"):
        shutil.copytree(FIXTURES / "mig02-relative", root / name)
    document = json.loads((root / "two/bundle.json").read_bytes())
    document["created_at"] = "2026-10-30T12:00:00Z"
    (root / "two/bundle.json").write_text(json.dumps(document))
    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, root))
        old = workspace.attach(incident, "one", "reviewer")[0]
        ref = track_options(workspace, incident, old)[0].track_ref
        map_track(
            workspace, incident, old.import_id, old.view_sha256, ref, "cannot_determine", None, "reviewer", "uncertain"
        )
        new = workspace.attach(incident, "two", "reviewer")[0]
        assert mapping_history(workspace, incident)[0].stale
        assert all(option.action == "unmapped" for option in track_options(workspace, incident, new))
        with pytest.raises(ValueError, match="current"):
            map_track(
                workspace, incident, old.import_id, old.view_sha256, ref, "cannot_determine", None, "reviewer", "again"
            )
        map_track(
            workspace,
            incident,
            new.import_id,
            new.view_sha256,
            ref,
            "cannot_determine",
            None,
            "reviewer",
            "reconfirmed",
            reconfirm_id=mapping_history(workspace, incident)[0].mapping_id,
        )
        assert len(mapping_history(workspace, incident)) == 2


def test_revision_supersedes_without_changing_original(
    database_url: str,
    incident: uuid.UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import hashlib
    import shutil

    root = tmp_path / "exports"
    shutil.copytree(FIXTURES / "mig02-relative", root / "one")
    shutil.copytree(root / "one", root / "two")
    document = json.loads((root / "two/bundle.json").read_bytes())
    document["created_at"] = "2026-10-30T12:00:00Z"
    (root / "two/bundle.json").write_text(json.dumps(document))
    before = {
        p.relative_to(root / "one").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "one").rglob("*")
        if p.is_file()
    }
    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, root))
        old = workspace.attach(incident, "one", "reviewer")[0]
        new = workspace.attach(incident, "two", "reviewer")[0]
        assert old.import_id != new.import_id
        assert workspace.current(incident) == (new,)
        assert workspace.history(incident)[0].superseded_by == new.link_id
        reattached = workspace.attach(incident, "one", "reviewer")[0]
        assert reattached == workspace.history(incident)[0]
        assert workspace.attachment_note(reattached) == f"this revision is superseded by {new.import_id}"
        assert workspace.decision(old.import_id).raw_artifacts["bundle.json"] == (root / "one/bundle.json").read_bytes()
    from incident_report.domain import Report, ReportView, Role
    from incident_report.reconstruction.cli import main
    from incident_report.reconstruction.presentation import context
    from incident_report.web.templating import build_environment

    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "true")
    monkeypatch.setenv("INCIDENTDESK_EVIDENCE_EXPORTS_ROOT", str(root))
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert main(["attach", "--incident", str(incident), "--attached-by", "reviewer", "one"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["note"] == f"this revision is superseded by {new.import_id}"
    report = Report(
        run_id=incident,
        input_hash="a" * 64,
        prompt_version="synthetic",
        provenance="synthetic",
        summary="Synthetic",
        views=(
            ReportView(
                filename="source.mp4",
                role=Role.EXTERIOR,
                label="Front",
                acquisition_method=None,
                sha256=old.view_sha256,
            ),
        ),
    )
    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, root))
        html = (
            build_environment()
            .get_template("review_timeline.html")
            .render(report=report, **context(workspace, report, workspace.settings))
        )
        assert f"this revision is superseded by {new.import_id}" in html
    assert before == {
        p.relative_to(root / "one").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "one").rglob("*")
        if p.is_file()
    }


def test_network_exports_root_is_refused_before_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_report.reconstruction.paths import bundle_path

    def forbidden(*args: object, **kwargs: object) -> bool:
        raise AssertionError("network path was probed")

    monkeypatch.setattr(Path, "is_symlink", forbidden)
    with pytest.raises(ValueError, match="local"):
        bundle_path(Path("//server/share"), "bundle")


def test_refused_attachment_retains_import_audit(database_url: str, incident: uuid.UUID) -> None:
    from incident_report.evidence_import import inspect_bundle

    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, FIXTURES))
        with pytest.raises(WorkspaceRefused, match="Rejected"):
            workspace.attach(incident, "negative/unknown-version", "reviewer")
        expected = inspect_bundle(FIXTURES / "negative/unknown-version")
        assert conn.execute(
            "SELECT provenance_state FROM evidence_imports WHERE import_id=%s", (expected.import_id,)
        ).fetchone() == ("rejected",)
        assert not workspace.current(incident)


def test_cli_refuses_external_database_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_report.reconstruction.cli import main

    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "true")
    monkeypatch.setenv("INCIDENTDESK_EVIDENCE_EXPORTS_ROOT", str(FIXTURES))
    monkeypatch.setenv("DATABASE_URL", "postgresql://user@outside.example/test")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("external connection attempted")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    with pytest.raises(SystemExit) as result:
        main(["attach", "--incident", str(uuid.uuid4()), "--attached-by", "reviewer", "mig02-relative"])
    assert result.value.code == 2


def test_older_review_cannot_replace_newer_review(database_url: str, incident: uuid.UUID, tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "exports"
    video = json.loads((FIXTURES / "mig02-001-01-rear-end/bundle.json").read_bytes())["sources"][0][
        "original_video_sha256"
    ]
    for revision, name in [("revision-001", "old"), ("revision-003", "new"), ("revision-002", "older-unattached")]:
        shutil.copytree(FIXTURES / "mig02-001-01-rear-end", root / name)
        path = root / name / "bundle.json"
        document = json.loads(path.read_bytes())
        source = document["sources"][0]
        source["review_revision"] = revision
        source["reviewed_manifest"] = source["reviewed_manifest"].replace("/revision-003/", f"/{revision}/")
        path.write_text(json.dumps(document))
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE runs SET report=%s::jsonb WHERE run_id=%s", (json.dumps({"views": [{"sha256": video}]}), incident)
        )
    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, root))
        old = workspace.attach(incident, "old", "reviewer")[0]
        new = workspace.attach(incident, "new", "reviewer")[0]
        assert workspace.history(incident)[0].superseded_by == new.link_id
        with pytest.raises(WorkspaceRefused, match="newer"):
            workspace.attach(incident, "older-unattached", "reviewer")
        assert workspace.current(incident) == (new,)
        assert workspace.decision(old.import_id).raw_artifacts["bundle.json"] == (root / "old/bundle.json").read_bytes()


def test_alignment_history_only_accepts_incident_views(database_url: str, incident: uuid.UUID) -> None:
    from incident_report.reconstruction.alignment import Alignment, history, record_alignment

    with psycopg.connect(database_url) as conn:
        workspace = Workspace(conn, ReconstructionSettings(True, FIXTURES))
        link = workspace.attach(incident, "mig02-relative", "reviewer")[0]
        assert history(workspace, incident) == ()
        with pytest.raises(ValueError, match="this incident"):
            record_alignment(
                workspace, incident, Alignment("b" * 64, link.view_sha256, 1.0, 0.5, "unknown", "reviewer", "estimate")
            )
        a = Alignment(link.view_sha256, link.view_sha256, 0.0, 0.5, "unknown", "reviewer", "reference")
        b = Alignment(link.view_sha256, link.view_sha256, 0.0, 0.1, "shared_clock_documented", "reviewer", "documented")
        record_alignment(workspace, incident, a)
        record_alignment(workspace, incident, b)
        assert history(workspace, incident) == (a, b)

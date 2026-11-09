"""Imported proposals join the existing cited timeline without changing Report data."""

from pathlib import Path

import pytest

from evidence_import_fixtures import FIXTURES, synthetic_bundle
from incident_report.evidence_import import inspect_bundle
from incident_report.reconstruction.timeline import timeline_groups


def test_flag_off_timeline_is_byte_identical() -> None:
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    env = build_environment()
    report = SimpleNamespace(
        views=[SimpleNamespace(sha256="a" * 64, label="Front", filename="front.mp4")], storyline=()
    )
    old = env.from_string((Path(__file__).parent / "fixtures/mig03_timeline_before.html").read_text())
    values = dict(report=report, citation=lambda item: "citation")
    assert env.get_template("review_timeline.html").render(**values) == old.render(**values)


@pytest.mark.parametrize("bundle", ["mig02-relative", "mig02-001-06-normal-crossing"])
def test_real_attachment_renders_without_network(
    database_url: str, monkeypatch: pytest.MonkeyPatch, bundle: str
) -> None:
    import json
    import socket
    import uuid

    import psycopg

    from incident_report.domain import Report, ReportView, Role
    from incident_report.reconstruction.config import ReconstructionSettings
    from incident_report.reconstruction.presentation import context
    from incident_report.reconstruction.store import Workspace
    from incident_report.web.templating import build_environment

    identifier = uuid.uuid4()
    sha = json.loads((FIXTURES / bundle / "bundle.json").read_bytes())["sources"][0]["original_video_sha256"]
    report = Report(
        run_id=identifier,
        input_hash="a" * 64,
        prompt_version="synthetic",
        provenance="synthetic",
        summary="Synthetic",
        views=(
            ReportView(filename="source.mp4", role=Role.EXTERIOR, label="Front", acquisition_method=None, sha256=sha),
        ),
    )
    settings = ReconstructionSettings(True, FIXTURES)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,origin,created_at,input_hash,report,status,total_latency_ms) VALUES (%s,'app',now(),%s,%s::jsonb,'succeeded',0)",
            (identifier, "a" * 64, report.model_dump_json()),
        )
        conn.commit()

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("network access attempted")

        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        workspace = Workspace(conn, settings)
        workspace.attach(identifier, bundle, "synthetic reviewer")
        values = context(workspace, report, settings)
        html = (
            build_environment()
            .get_template("review_timeline.html")
            .render(report=report, csrf_token=uuid.uuid4().hex, review_workspace=True, **values)
        )
        if bundle == "mig02-relative":
            assert "Reconstruction proposal" in html
            assert "order not determined" in html.lower()
            assert "data-seconds=" in html
        else:
            assert "Reconstruction could not assess this view" in html
            assert "assessment is unavailable" in html
            assert "No Incident" not in html
        assert 'name="bundle"' in html


def test_real_proposals_have_citations_and_origin() -> None:
    decision = inspect_bundle(FIXTURES / "mig02-relative")
    assert decision.envelope is not None
    source = decision.envelope.sources[0]
    groups = timeline_groups(decision, source.view_id, source.original_video_sha256, ())
    rows = [row for group in groups for row in group.rows]
    assert rows and all(row.origin == "Reconstruction proposal" for row in rows)
    assert all(row.view_id == source.original_video_sha256 for row in rows)
    assert any("lost" in row.track_states for row in rows)
    assert any(group.order_unresolved for group in groups)


def test_real_multi_track_events_keep_distinct_tool_labels_and_all_participants(database_url: str) -> None:
    import uuid

    import psycopg

    from incident_report.domain import Report, ReportView, Role
    from incident_report.facts import OverviewFacts, PartyFact
    from incident_report.reconstruction.config import ReconstructionSettings
    from incident_report.reconstruction.mapping import map_track
    from incident_report.reconstruction.presentation import context
    from incident_report.reconstruction.store import Workspace

    decision = inspect_bundle(FIXTURES / "mig02-multi-review")
    assert decision.envelope is not None
    sha = decision.envelope.sources[0].original_video_sha256
    identifier = uuid.uuid4()
    report = Report(
        run_id=identifier,
        input_hash="a" * 64,
        prompt_version="synthetic",
        provenance="synthetic",
        summary="Synthetic",
        views=(
            ReportView(filename="source.avi", role=Role.EXTERIOR, label="Front", acquisition_method=None, sha256=sha),
        ),
        facts=OverviewFacts(
            parties=[
                PartyFact(
                    description="Synthetic white Toyota sedan",
                    party_code="P1",
                    view_id=sha,
                    timestamp_seconds=0.0,
                    confidence=1.0,
                )
            ],
            events=[],
            candidate_collisions=[],
            behaviour_observations=[],
            location_conditions=[],
        ),
    )
    settings = ReconstructionSettings(True, FIXTURES)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,origin,created_at,input_hash,report,status,total_latency_ms) VALUES (%s,'replay',now(),%s,%s::jsonb,'succeeded',0)",
            (identifier, report.input_hash, report.model_dump_json()),
        )
        workspace = Workspace(conn, settings)
        workspace.attach(identifier, "mig02-multi-review", "synthetic reviewer")
        panel = context(workspace, report, settings)["reconstruction"][sha]
        assert {track.track_id for track in panel.tracks} == {1, 2, 3, 4}
        assert len({track.label for track in panel.tracks}) == 4
        for track in panel.tracks:
            assert f"tool track identifier {track.track_id}" in track.label
            assert track.label.startswith("Unmapped reconstruction track (")
        collision = next(
            row for group in panel.groups for row in group.rows if row.text.startswith("Possible collision")
        )
        assert len(collision.track_refs) == 2
        assert collision.text.count("Unmapped reconstruction track (") == 2
        labels = {track.track_ref: track.label for track in panel.tracks}
        assert all(labels[ref] in collision.text for ref in collision.track_refs)
        for track in panel.tracks:
            if track.track_ref in collision.track_refs:
                map_track(
                    workspace,
                    identifier,
                    track.import_id,
                    sha,
                    track.track_ref,
                    "map",
                    "P1",
                    "synthetic reviewer",
                    "synthetic display test",
                )
        mapped_panel = context(workspace, report, settings)["reconstruction"][sha]
        mapped_collision = next(row for group in mapped_panel.groups for row in group.rows if row.key == collision.key)
        assert mapped_collision.text.count("Synthetic white Toyota sedan") == 2


def test_rejected_proposal_never_gets_confirmed_badge(tmp_path: Path) -> None:
    decision = inspect_bundle(synthetic_bundle(tmp_path, rejected=True))
    assert decision.envelope is not None
    source = decision.envelope.sources[0]
    rows = [
        r for group in timeline_groups(decision, source.view_id, source.original_video_sha256, ()) for r in group.rows
    ]
    rejected = [r for r in rows if r.rejected]
    assert rejected and all(r.review_status == "Rejected by reconstruction reviewer" for r in rejected)


def test_modified_unknown_time_never_cites_original_frame(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    decision = inspect_bundle(synthetic_bundle(tmp_path, modified=True))
    assert decision.envelope is not None
    source = decision.envelope.sources[0]
    groups = timeline_groups(decision, source.view_id, source.original_video_sha256, ())
    modified = [r for g in groups for r in g.rows if r.review_status == "Modified by reconstruction reviewer"]
    assert modified
    assert all(r.source_frame is None and r.source_time is None and r.needs_review for r in modified)
    assert all(r.original_time is not None and r.requested_time is not None for r in modified)
    html = (
        build_environment()
        .get_template("reconstruction_view.html")
        .render(panel=SimpleNamespace(groups=groups, notices=(), links=()), print_view=False, review_workspace=True)
    )
    assert "unknown — no frame citation" in html


def test_modified_status_without_overlay_never_cites_original_frame() -> None:
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    original = inspect_bundle(FIXTURES / "mig02-relative")
    decision = original.model_copy(
        update={
            "records": tuple(
                r.model_copy(update={"producer_status": "MODIFIED_BY_REVIEWER"}) if r.kind == "event_proposal" else r
                for r in original.records
                if r.kind != "review_overlay"
            )
        }
    )
    assert decision.envelope is not None
    source = decision.envelope.sources[0]
    groups = timeline_groups(decision, source.view_id, source.original_video_sha256, ())
    rows = [r for g in groups for r in g.rows]
    assert rows and all(r.source_frame is None and r.source_time is None and r.needs_review for r in rows)
    html = (
        build_environment()
        .get_template("reconstruction_view.html")
        .render(panel=SimpleNamespace(groups=groups, notices=(), links=()), print_view=False, review_workspace=True)
    )
    assert "corrected time unknown — no frame citation" in html
    assert "data-seconds=" not in html


def test_seek_controls_only_appear_with_the_existing_player() -> None:
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    decision = inspect_bundle(FIXTURES / "mig02-relative")
    assert decision.envelope is not None
    source = decision.envelope.sources[0]
    groups = timeline_groups(decision, source.view_id, source.original_video_sha256, ())
    template = build_environment().get_template("reconstruction_view.html")
    values = dict(panel=SimpleNamespace(groups=groups, notices=(), links=()), print_view=False)
    assert "data-seconds=" not in template.render(review_workspace=False, **values)
    assert "data-seconds=" in template.render(review_workspace=True, **values)
    assert "lost at some point in this clip" in template.render(review_workspace=True, **values)
    assert "interpolated at some point in this clip" in template.render(review_workspace=True, **values)


def test_app_cited_moment_renders_without_an_empty_review_badge() -> None:
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    sha = "a" * 64
    marker = SimpleNamespace(view_id=sha, timestamp_seconds=1.25, text="Synthetic app marker")
    groups = timeline_groups(None, "", sha, (marker,))
    html = (
        build_environment()
        .get_template("reconstruction_view.html")
        .render(panel=SimpleNamespace(groups=groups, notices=(), links=()), print_view=False, review_workspace=True)
    )
    assert "App cited moment" in html and "Synthetic app marker" in html
    assert 'data-seconds="1.25"' in html
    assert "<strong>" not in html


def test_unavailable_notice_is_excluded_from_browser_print() -> None:
    from html.parser import HTMLParser
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    class VisibleText(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hidden: list[bool] = []
            self.printed: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.hidden.append(any(self.hidden) or "reconstruction-only" in (dict(attrs).get("class") or "").split())

        def handle_endtag(self, tag: str) -> None:
            if self.hidden:
                self.hidden.pop()

        def handle_data(self, data: str) -> None:
            if not any(self.hidden):
                self.printed.append(data)

    html = (
        build_environment()
        .get_template("review_timeline.html")
        .render(
            reconstruction_unavailable="Local workspace unavailable",
            print_view=False,
            report=SimpleNamespace(views=(), storyline=()),
        )
    )
    assert "Local workspace unavailable" in html
    browser_print = VisibleText()
    browser_print.feed(html)
    assert "Local workspace unavailable" not in "".join(browser_print.printed)


def test_browser_print_retains_original_app_citations() -> None:
    from types import SimpleNamespace

    from incident_report.web.templating import build_environment

    sha = "a" * 64
    moment = SimpleNamespace(view_id=sha, timestamp_seconds=2.5, kind="event")
    view = SimpleNamespace(sha256=sha, label="Front", filename="front.mp4")
    html = build_environment().get_template("reconstruction_print_timeline.html").render(view=view, moments=[moment])
    assert 'class="reconstruction-print-fallback" hidden' in html
    assert sha + " @ 2.5s" in html and "Event" in html
    assert "data-seconds=" not in html and "Reconstruction proposal" not in html


def test_imported_reason_is_escaped_and_pdf_excludes_it(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from typing import Any

    from incident_report.web.templating import build_environment

    def change(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for rows in documents.values():
            for row in rows:
                if "document" in row and "decisions" in row["document"]:
                    for decision in row["document"]["decisions"]:
                        decision["note"] = "<script>attack()</script>"
                if row.get("decision") is not None:
                    row["decision"]["note"] = "<script>attack()</script>"

    decision = inspect_bundle(synthetic_bundle(tmp_path, transform=change))
    assert decision.envelope is not None
    source = decision.envelope.sources[0]
    groups = timeline_groups(decision, source.view_id, source.original_video_sha256, ())
    env = build_environment()
    template = env.get_template("reconstruction_view.html")
    html = template.render(
        panel=SimpleNamespace(groups=groups, notices=(), links=()),
        view=SimpleNamespace(sha256=source.original_video_sha256),
        print_view=False,
    )
    assert "&lt;script&gt;" in html and "<script>attack()" not in html
    assert (
        template.render(
            panel=SimpleNamespace(groups=groups, notices=(), links=()),
            view=SimpleNamespace(sha256=source.original_video_sha256),
            print_view=True,
        ).strip()
        == ""
    )

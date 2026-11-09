"""Reviewer uncertainty bounds determine display order, never source citations."""

import pytest


def test_order_needs_same_reference_and_strictly_separated_intervals() -> None:
    from incident_report.reconstruction.alignment import Alignment, compare

    a = Alignment("a", "r", 1.0, 0.2, "reviewer_estimated", "reviewer", "clapper")
    b = Alignment("b", "r", 0.0, 0.3, "reviewer_estimated", "reviewer", "estimate")
    assert compare(2.0, a, 4.0, b) == "before"
    assert compare(2.0, a, 3.5, b) == "order across cameras not determined"
    assert compare(2.0, a, 3.499, b) == "order across cameras not determined"
    assert compare(2.0, a, 2.0, b) == "after"
    assert compare(2.0, None, 4.0, b) == "order across cameras not determined"
    other = Alignment("b", "other", 0.0, 0.0, "unknown", "reviewer", "estimate")
    assert compare(2.0, a, 4.0, other) == "order across cameras not determined"
    unknown = Alignment("b", "r", 0.0, 0.0, "unknown", "reviewer", "unknown clock")
    assert compare(2.0, a, 10.0, unknown) == "order across cameras not determined"


@pytest.mark.parametrize("offset,uncertainty", [(float("nan"), 0.0), (0.0, float("inf")), (0.0, -1.0)])
def test_nonfinite_or_negative_uncertainty_refused(offset: float, uncertainty: float) -> None:
    from incident_report.reconstruction.alignment import Alignment

    with pytest.raises(ValueError):
        Alignment("a", "r", offset, uncertainty, "unknown", "reviewer", "reason")


def test_alignment_required_uncertainty_and_small_reliability_vocabulary() -> None:
    from incident_report.reconstruction.alignment import Alignment

    with pytest.raises(TypeError):
        Alignment(view="a", reference="r", offset=0.0, clock_reliability="unknown", reviewer="r", reason="test")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Alignment("a", "r", 0.0, 0.0, "trusted", "reviewer", "reason")


def test_fault_and_ai_have_no_workspace_reads() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/incident_report"
    for name in ("rules", "ai"):
        for path in (root / name).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "track_party_mappings" not in text and "view_time_alignments" not in text
            assert "incident_report.reconstruction" not in text


def test_combined_groups_keep_citations_native_and_exclude_unresolved_proposals() -> None:
    from types import SimpleNamespace

    from incident_report.reconstruction.alignment import Alignment, combined_groups
    from incident_report.reconstruction.timeline import TimelineGroup, timeline_groups

    a = SimpleNamespace(view_id="a", timestamp_seconds=1.0, text="A")
    b = SimpleNamespace(view_id="b", timestamp_seconds=2.0, text="B")
    groups_a = timeline_groups(None, "", "a", (a,))
    groups_b = timeline_groups(None, "", "b", (b,))
    panels = {"a": SimpleNamespace(groups=groups_a), "b": SimpleNamespace(groups=groups_b)}
    alignments = (
        Alignment("a", "a", 0.0, 0.5, "reviewer_estimated", "r", "reason"),
        Alignment("b", "a", 0.0, 0.5, "reviewer_estimated", "r", "reason"),
    )
    assert combined_groups(panels, ()) == ()
    combined = combined_groups(panels, alignments)
    assert len(combined) == 1 and combined[0].order_unresolved
    assert combined[0].rows[1].row.source_time == 2.0
    assert combined[0].rows[1].row.view_id == "b"
    panels["b"] = SimpleNamespace(groups=(TimelineGroup(groups_b[0].rows, True),))
    assert combined_groups(panels, alignments) == ()


def test_same_view_overlap_keeps_native_order_without_cross_camera_label() -> None:
    from types import SimpleNamespace

    from incident_report.reconstruction.alignment import Alignment, combined_groups
    from incident_report.reconstruction.timeline import timeline_groups

    moments = tuple(
        SimpleNamespace(view_id=v, timestamp_seconds=t, text=str(t)) for v, t in (("a", 2.0), ("a", 1.0), ("b", 20.0))
    )
    panels = {v: SimpleNamespace(groups=timeline_groups(None, "", v, moments)) for v in ("a", "b")}
    alignments = tuple(Alignment(v, "a", 0.0, 2.0, "reviewer_estimated", "r", "reason") for v in panels)
    groups = combined_groups(panels, alignments)
    assert all(not group.order_unresolved for group in groups)
    assert [a.row.source_time for g in groups for a in g.rows if a.row.view_id == "a"] == [1.0, 2.0]


@pytest.mark.parametrize("front_label", ["Front camera", "<b>Front camera</b>"])
def test_alignment_history_uses_view_labels_with_hashes_secondary(front_label: str) -> None:
    from html import escape
    from types import SimpleNamespace

    from incident_report.reconstruction.alignment import Alignment
    from incident_report.web.templating import build_environment

    front, rear = "a" * 64, "b" * 64
    html = (
        build_environment()
        .get_template("reconstruction_alignment.html")
        .render(
            report=SimpleNamespace(
                run_id="synthetic",
                views=(
                    SimpleNamespace(sha256=front, label=front_label, filename="front.mp4"),
                    SimpleNamespace(sha256=rear, label=None, filename="rear.mp4"),
                ),
            ),
            reconstruction_alignments=(
                Alignment(front, rear, 1.0, 0.5, "reviewer_estimated", "reviewer", "synthetic"),
            ),
            reconstruction_clocks=("unknown", "reviewer_estimated", "shared_clock_documented"),
            reconstruction_combined=(),
        )
    )
    assert f"View {escape(front_label)}" in html
    assert "to reference rear.mp4" in html
    assert f'<span class="meta">({front[:12]})</span>' in html
    assert f'<span class="meta">({rear[:12]})</span>' in html
    assert "<b>Front camera</b>" not in html

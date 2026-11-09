"""Owner approval (RPT-02): a label counts only if the owner applied it and it is still on.

CI downloads the PR's label history from the GitHub issue-events API and hands the JSON to
guard_approval.py. These tests feed it the same shape of data. Without the "who applied it" check, any
account that can label a PR (an agent's own token, for one) could approve its own change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import guard_approval

LABEL = "owner-approved-test-change"


def event(kind: str, label: str, actor: str | None, when: str, event_id: int) -> dict[str, object]:
    return {
        "id": event_id,
        "event": kind,
        "created_at": when,
        "actor": None if actor is None else {"login": actor},
        "label": {"name": label},
    }


def test_the_label_applied_by_the_owner_is_approval() -> None:
    events = [event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1)]

    assert guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_the_label_applied_by_someone_else_is_not_approval() -> None:
    events = [event("labeled", LABEL, "some-agent-token", "2026-10-13T10:00:00Z", 1)]

    assert not guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_a_label_the_owner_removed_again_is_not_approval() -> None:
    events = [
        event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1),
        event("unlabeled", LABEL, "the-owner", "2026-10-13T11:00:00Z", 2),
    ]

    assert not guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_the_owner_labelling_again_after_removing_it_is_approval() -> None:
    events = [
        event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1),
        event("unlabeled", LABEL, "the-owner", "2026-10-13T11:00:00Z", 2),
        event("labeled", LABEL, "the-owner", "2026-10-13T12:00:00Z", 3),
    ]

    assert guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_someone_else_re_adding_the_label_after_the_owner_removed_it_is_not_approval() -> None:
    events = [
        event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1),
        event("unlabeled", LABEL, "the-owner", "2026-10-13T11:00:00Z", 2),
        event("labeled", LABEL, "an-agent", "2026-10-13T12:00:00Z", 3),
    ]

    assert not guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_events_are_judged_in_time_order_even_if_the_api_lists_them_out_of_order() -> None:
    events = [
        event("unlabeled", LABEL, "the-owner", "2026-10-13T11:00:00Z", 2),
        event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1),
    ]

    assert not guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_other_labels_and_other_events_are_ignored() -> None:
    events = [
        event("labeled", "owner-approved-protected-paths", "the-owner", "2026-10-13T10:00:00Z", 1),
        {"id": 2, "event": "assigned", "created_at": "2026-10-13T10:05:00Z", "actor": {"login": "the-owner"}},
    ]

    assert not guard_approval.is_approved(events, LABEL, {"the-owner"})


def test_login_comparison_ignores_letter_case_and_a_deleted_actor_never_approves() -> None:
    assert guard_approval.is_approved(
        [event("labeled", LABEL, "The-Owner", "2026-10-13T10:00:00Z", 1)], LABEL, {"the-owner"}
    )
    assert not guard_approval.is_approved(
        [event("labeled", LABEL, None, "2026-10-13T10:00:00Z", 1)], LABEL, {"the-owner"}
    )


def test_no_events_is_no_approval() -> None:
    assert not guard_approval.is_approved([], LABEL, {"the-owner"})


def test_reads_both_shapes_of_gh_api_paginate_output(tmp_path: Path) -> None:
    one = event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1)
    two = event("labeled", "other", "the-owner", "2026-10-13T10:00:00Z", 2)
    slurped = tmp_path / "slurped.json"
    slurped.write_text(json.dumps([[two], [one]]))  # gh api --paginate --slurp: a list of pages
    concatenated = tmp_path / "concatenated.json"
    concatenated.write_text(json.dumps([two]) + json.dumps([one]))  # gh api --paginate: pages back to back

    assert guard_approval.load_events(slurped) == guard_approval.load_events(concatenated)
    assert guard_approval.is_approved(guard_approval.load_events(slurped), LABEL, {"the-owner"})


def test_main_writes_one_output_line_per_check_for_github_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps([event("labeled", LABEL, "the-owner", "2026-10-13T10:00:00Z", 1)]))
    github_output = tmp_path / "github_output"

    code = guard_approval.main(
        [
            "--owners", "The-Owner",
            "--check", f"tests={LABEL}",
            "--check", "protected=owner-approved-protected-paths",
            "--github-output", str(github_output),
            str(events_file),
        ]
    )  # fmt: skip

    assert code == 0
    assert github_output.read_text().splitlines() == ["tests=true", "protected=false"]
    assert "tests: approved" in capsys.readouterr().out


def test_main_with_no_events_file_means_no_approval_such_as_a_push_with_no_pull_request(tmp_path: Path) -> None:
    github_output = tmp_path / "github_output"

    code = guard_approval.main(
        ["--owners", "the-owner", "--check", f"tests={LABEL}", "--github-output", str(github_output)]
    )

    assert code == 0
    assert github_output.read_text().splitlines() == ["tests=false"]


def test_main_with_an_unreadable_events_file_is_an_error_not_an_approval(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")

    assert guard_approval.main(["--owners", "o", "--check", f"tests={LABEL}", str(broken)]) == 2


def test_main_needs_at_least_one_owner() -> None:
    with pytest.raises(SystemExit):
        guard_approval.main(["--owners", " , ", "--check", f"tests={LABEL}"])

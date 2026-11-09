"""RPT-38: Override, Approve, lock and versions, tested through the HTTP routes and the RunLog.

Every scenario runs behind a real share session. Database facts (what was stored, what the audit log holds)
are read back with the RunLog / AuditLog or plain SQL, never through the code under test.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from incident_report.access import AccessStore
from incident_report.ai import AIConnection, ReplayAIConnection
from incident_report.audit import AuditEntry
from incident_report.domain import Attribution, IncidentRow, Origin, Report, ReportView, Role, Run, RunStatus
from incident_report.facts import CitedFact, OverviewFacts
from incident_report.rules.models import FaultStatus, PartyFault, PartyViolation
from incident_report.runlog import RunLog
from incident_report.web import create_app

HASH = "a" * 64
SCRIPT = "<script>alert('x')</script>"
OVERRIDE_FORM = {"finding_key": "party:0", "new_value": "Not at Fault", "reason_code": "other", "reason_text": "x"}


class Session:
    """One browser with a real share-link session, plus the ids the audit log must carry."""

    def __init__(self, client: TestClient, session_id: str, link_id: str) -> None:
        self.client = client
        self.session_id = uuid.UUID(session_id)
        self.link_id = uuid.UUID(link_id)


def _open_session(app: Any, database_url: str) -> Session:
    link = AccessStore(database_url).create_link()
    client = TestClient(app, base_url="https://testserver")
    response = client.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]})
    assert response.status_code == 200
    body = response.json()
    return Session(client, body["session_id"], body["link_id"])


def _fact() -> CitedFact:
    return CitedFact(description="Contact at the junction", view_id=HASH, timestamp_seconds=4.0, confidence=1.0)


def _report(*, is_valid: bool = True) -> Report:
    fact = _fact()
    at_fault = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id="r1",
        legal_source="MR 178/2017",
        article="Article 1",
        reason="Failed to keep a safe distance",
        cited_facts=(fact,),
    )
    not_at_fault = PartyFault(
        party_description="white Nissan SUV",
        status=FaultStatus.NOT_AT_FAULT,
        color="green",
        reason="Was stationary",
        cited_facts=(fact,),
    )
    violation = PartyViolation(
        party_description="silver Toyota sedan",
        rule_id="v1",
        item_number="item-1",
        name_en="Tailgating",
        aed=400,
        black_points=4,
        source_url="https://example.com/source",
        status="verified",
        cited_facts=(fact,),
    )
    return Report(
        run_id=uuid.uuid4(),
        input_hash=HASH,
        prompt_version="synthetic-decisions",
        provenance="synthetic",
        summary="Decision fixture",
        views=(
            ReportView(
                filename="a.mp4",
                role=Role.EXTERIOR,
                label="Front camera",
                acquisition_method=None,
                sha256=HASH,
                duration_seconds=10.0,
            ),
        ),
        facts=OverviewFacts(
            parties=[],
            events=[],
            candidate_collisions=[fact],
            behaviour_observations=[],
            location_conditions=[],
        ),
        parties=(at_fault, not_at_fault),
        violations=(violation,),
        is_valid=is_valid,
    )


def _record(run_log: RunLog, report: Report, attribution: Attribution | None = None) -> None:
    run_log.record(
        Run(
            run_id=report.run_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash=HASH,
            report=report,
            status=RunStatus.SUCCEEDED,
            total_latency_ms=0,
            attribution=attribution,
        )
    )


@pytest.fixture
def report(run_log: RunLog) -> Report:
    r = _report()
    _record(run_log, r)
    return r


@pytest.fixture
def app(run_log: RunLog) -> Any:
    return create_app(ai=ReplayAIConnection(), run_log=run_log, evidence=None)


@pytest.fixture
def session(app: Any, run_log: RunLog) -> Session:
    return _open_session(app, run_log.database_url)


def _token(session: Session, report: Report) -> str:
    html = session.client.get(f"/reports/{report.run_id}/review").text
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', html)
    assert match, "the review workspace must carry a CSRF token"
    return match.group(1)


def _post(
    session: Session,
    report: Report,
    path: str,
    data: dict[str, str] | None = None,
    *,
    token: str | None = None,
    run_id: uuid.UUID | None = None,
    **kwargs: Any,
) -> Any:
    form = {"csrf_token": token or _token(session, report), **(data or {})}
    return session.client.post(
        f"/reports/{run_id or report.run_id}/{path}", data=form, follow_redirects=False, **kwargs
    )


def _override(
    session: Session,
    report: Report,
    *,
    key: str = "party:0",
    value: str = "Not at Fault",
    reason: str = "video_shows_otherwise",
    text: str = "The second camera shows the other car braking late.",
    extra: dict[str, str] | None = None,
    token: str | None = None,
    run_id: uuid.UUID | None = None,
) -> Any:
    data = {"finding_key": key, "new_value": value, "reason_code": reason, "reason_text": text, **(extra or {})}
    return _post(session, report, "overrides", data, token=token, run_id=run_id)


def _rows(run_log: RunLog, table: str, report: Report) -> list[tuple[Any, ...]]:
    with psycopg.connect(run_log.database_url) as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE run_id = %s",  # noqa: S608  # literal table names
            (report.run_id,),
        ).fetchall()


def _audit(run_log: RunLog, report: Report, action: str) -> list[AuditEntry]:
    return [e for e in run_log.audit.list_entries() if e.run_id == report.run_id and e.action == action]


# --- Override -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason_code": ""},
        {"reason_code": "made_up_category"},
        {"reason_text": ""},
        {"reason_text": "   \n\t "},
        {"reason_text": "x" * 1001},
        {"reason_text": "bad\x00text"},
        {"finding_key": "party:9"},
        {"finding_key": "violation:1"},
        {"finding_key": "summary"},
        {"finding_key": "party:0; DROP TABLE runs"},
        {"new_value": "Maybe at fault"},
        {"new_value": "At Fault"},  # already the AI's value: nothing to override
        {"new_value": ""},
    ],
)
def test_an_override_without_a_valid_reason_or_target_is_refused_and_stores_nothing(
    run_log: RunLog, report: Report, session: Session, overrides: dict[str, str]
) -> None:
    response = _override(
        session,
        report,
        key=overrides.get("finding_key", "party:0"),
        value=overrides.get("new_value", "Not at Fault"),
        reason=overrides.get("reason_code", "video_shows_otherwise"),
        text=overrides.get("reason_text", "Valid free text."),
    )

    assert response.status_code == 422
    assert _rows(run_log, "report_overrides", report) == []
    assert _audit(run_log, report, "override.created") == []


def test_a_missing_reason_field_is_refused(run_log: RunLog, report: Report, session: Session) -> None:
    data = {"finding_key": "party:0", "new_value": "Not at Fault"}  # neither reason_code nor reason_text sent

    response = _post(session, report, "overrides", data)

    assert response.status_code == 422
    assert _rows(run_log, "report_overrides", report) == []


def test_an_override_keeps_the_ai_original_visible_and_stored(
    run_log: RunLog, report: Report, session: Session
) -> None:
    before = run_log.get(report.run_id).report

    response = _override(session, report)

    assert response.status_code == 303
    assert run_log.get(report.run_id).report == before  # the AI's report itself is never rewritten
    with psycopg.connect(run_log.database_url) as conn:
        rows = conn.execute(
            "SELECT ai_original, new_value, reason_code, reason_text, version_number FROM report_overrides "
            "WHERE run_id = %s",
            (report.run_id,),
        ).fetchall()
    assert len(rows) == 1
    ai_original, new_value, reason_code, reason_text, version = rows[0]
    assert ai_original["fault_status"] == "At Fault" and ai_original["party_description"] == "silver Toyota sedan"
    assert (new_value, reason_code, version) == ("Not at Fault", "video_shows_otherwise", 1)
    assert reason_text == "The second camera shows the other car braking late."

    page = session.client.get(f"/reports/{report.run_id}/review").text
    original = re.search(r'<[^>]*class="[^"]*ai-original[^"]*"[^>]*>(.*?)</', page, re.DOTALL)
    assert original and "At Fault" in original.group(1)  # greyed original still on the page ...
    assert "Officer override" in page and "Not at Fault" in page  # ... beside the override
    assert "Video shows otherwise" in page and "braking late" in page


def test_a_violation_can_be_overridden_and_the_page_says_the_totals_are_the_ais(
    report: Report, session: Session
) -> None:
    response = _override(session, report, key="violation:0", value="Not established", reason="wrong_party", text="x")

    assert response.status_code == 303
    page = session.client.get(f"/reports/{report.run_id}").text
    assert "Not established" in page
    assert "totals above are the ai" in page.lower()


def test_overriding_the_same_finding_twice_keeps_both_rows_and_shows_the_newest(
    run_log: RunLog, report: Report, session: Session
) -> None:
    assert _override(session, report, value="Not at Fault", text="first opinion").status_code == 303
    assert _override(session, report, value="Cannot Determine", text="second opinion").status_code == 303

    assert len(_rows(run_log, "report_overrides", report)) == 2  # history is append-only
    page = session.client.get(f"/reports/{report.run_id}/review").text
    assert "second opinion" in page and "first opinion" not in page


def test_override_reasons_are_plain_ui_reasons(session: Session, report: Report) -> None:
    page = session.client.get(f"/reports/{report.run_id}/review").text

    for label in ("Video shows otherwise", "Wrong party", "Wrong timestamp", "Other"):
        assert label in page


# --- Approve and the lock --------------------------------------------------------------------------------


def test_approve_locks_the_report_and_refuses_further_overrides(
    run_log: RunLog, report: Report, session: Session
) -> None:
    token = _token(session, report)
    assert _override(session, report).status_code == 303

    assert _post(session, report, "approve").status_code == 303

    page = session.client.get(f"/reports/{report.run_id}/review").text
    assert "Approved" in page and "read-only" in page.lower()
    assert 'name="reason_text"' not in page  # no override form on a locked version
    before = _rows(run_log, "report_overrides", report)
    refused = _override(
        session, report, key="party:1", value="At Fault", reason="wrong_party", text="late change", token=token
    )
    assert refused.status_code == 409
    assert _rows(run_log, "report_overrides", report) == before


def test_approving_twice_is_refused(session: Session, report: Report, run_log: RunLog) -> None:
    token = _token(session, report)
    assert _post(session, report, "approve", token=token).status_code == 303

    assert _post(session, report, "approve", token=token).status_code == 409
    assert len(_rows(run_log, "report_approvals", report)) == 1


def test_an_invalid_report_cannot_be_approved_or_overridden(run_log: RunLog, report: Report, session: Session) -> None:
    token = _token(session, report)
    invalid = _report(is_valid=False)
    _record(run_log, invalid)

    assert _post(session, invalid, "approve", token=token).status_code == 409
    assert _override(session, invalid, token=token).status_code == 409
    assert _rows(run_log, "report_approvals", invalid) == []
    assert _rows(run_log, "report_overrides", invalid) == []


def test_a_report_that_is_not_ready_has_no_decisions(session: Session, report: Report) -> None:
    token = _token(session, report)
    ghost = uuid.uuid4()

    assert _override(session, report, token=token, run_id=ghost).status_code == 404
    assert _post(session, report, "approve", token=token, run_id=ghost).status_code == 404
    assert _post(session, report, "versions", token=token, run_id=ghost).status_code == 404


# --- Versions -----------------------------------------------------------------------------------------


def test_an_override_after_approval_creates_version_two_and_leaves_version_one_unchanged(
    run_log: RunLog, report: Report, session: Session
) -> None:
    assert _override(session, report, text="before approval").status_code == 303
    assert _post(session, report, "approve").status_code == 303
    version_one_before = run_log.decisions.state(report.run_id, 1)

    response = _override(
        session,
        report,
        key="party:1",
        value="At Fault",
        reason="wrong_party",
        text="after approval",
        extra={"new_version": "1"},
    )

    assert response.status_code == 303
    version_one_after = run_log.decisions.state(report.run_id, 1)
    assert version_one_after.overrides == version_one_before.overrides  # version 1 reads exactly as it did
    assert version_one_after.current == version_one_before.current  # still approved, same approval time
    v2 = session.client.get(f"/reports/{report.run_id}/review").text
    assert "Version 2" in v2 and "after approval" in v2 and "before approval" in v2  # v2 inherits v1's override
    v1_again = session.client.get(f"/reports/{report.run_id}/review?version=1").text
    assert "before approval" in v1_again and "after approval" not in v1_again
    assert "Approved" in v1_again
    with psycopg.connect(run_log.database_url) as conn:
        versions = conn.execute(
            "SELECT version_number, parent_version FROM report_versions WHERE run_id = %s", (report.run_id,)
        ).fetchall()
        v1_overrides = conn.execute(
            "SELECT count(*) FROM report_overrides WHERE run_id = %s AND version_number = 1", (report.run_id,)
        ).fetchone()
    assert versions == [(2, 1)]
    assert v1_overrides == (1,)  # the approved version was never added to


def test_a_new_version_can_be_started_explicitly_only_after_approval(
    run_log: RunLog, report: Report, session: Session
) -> None:
    token = _token(session, report)
    assert _post(session, report, "versions", token=token).status_code == 409  # nothing approved yet
    assert _post(session, report, "approve", token=token).status_code == 303

    assert _post(session, report, "versions", token=token).status_code == 303

    page = session.client.get(f"/reports/{report.run_id}/review").text
    assert "Version 2" in page and 'name="reason_text"' in page  # version 2 is a draft with the form back
    assert _post(session, report, "versions", token=token).status_code == 409  # version 2 is not approved yet
    assert _post(session, report, "approve", token=token).status_code == 303
    assert len(_rows(run_log, "report_approvals", report)) == 2


def test_an_unknown_version_is_a_404(session: Session, report: Report) -> None:
    for path in ("/review", "/print", ""):
        assert session.client.get(f"/reports/{report.run_id}{path}?version=7").status_code == 404
    assert session.client.get(f"/reports/{report.run_id}/review?version=abc").status_code == 422


def test_the_plain_report_and_the_print_view_show_the_override_but_no_forms(report: Report, session: Session) -> None:
    assert _override(session, report).status_code == 303

    for path in ("", "/print"):
        page = session.client.get(f"/reports/{report.run_id}{path}").text
        assert "Officer override" in page and "braking late" in page
        assert 'name="reason_text"' not in page
        assert "ai-original" in page


# --- Audit ----------------------------------------------------------------------------------------------


def test_override_approve_and_new_version_are_audited_with_session_attribution_and_the_chain_holds(
    run_log: RunLog, report: Report, session: Session
) -> None:
    assert _override(session, report).status_code == 303
    assert _post(session, report, "approve").status_code == 303
    second = _override(
        session, report, key="party:1", value="At Fault", reason="other", text="new facts", extra={"new_version": "1"}
    )
    assert second.status_code == 303

    first_override, new_version_override = _audit(run_log, report, "override.created")
    (approved,) = _audit(run_log, report, "report.approved")
    (version_entry,) = _audit(run_log, report, "version.created")
    for entry in (first_override, approved, version_entry, new_version_override):
        assert entry.actor == "visitor"
        assert entry.session_id == session.session_id and entry.link_id == session.link_id
        assert entry.request_id is not None
        assert entry.target_type == "report_version"
    assert first_override.details["finding_key"] == "party:0"
    assert first_override.details["reason_code"] == "video_shows_otherwise"
    assert first_override.details["version_number"] == 1
    assert approved.details["version_number"] == 1 and approved.details["override_count"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", approved.details["overrides_digest"])
    assert approved.details["input_hash"] == HASH
    assert version_entry.details == {"version_number": 2, "parent_version": 1}
    assert new_version_override.details["version_number"] == 2
    order = [e.sequence_number for e in (first_override, approved, version_entry, new_version_override)]
    assert order == sorted(order)  # recorded in the order they happened
    assert run_log.audit.verify().valid  # the whole chain still verifies


def test_the_audit_log_holds_a_fingerprint_of_the_officer_text_not_the_text(
    run_log: RunLog, report: Report, session: Session
) -> None:
    note = "witness name Aisha Rahman, phone 050-000-0000"
    assert _override(session, report, text=note).status_code == 303

    (entry,) = _audit(run_log, report, "override.created")
    assert note not in str(entry.details) and "Aisha" not in str(entry.details)
    assert re.fullmatch(r"[0-9a-f]{64}", entry.details["content_sha256"])
    assert uuid.UUID(entry.details["override_id"])


def test_a_refused_request_writes_no_decision_audit_row(run_log: RunLog, report: Report, session: Session) -> None:
    token = _token(session, report)
    _override(session, report, reason="", token=token)
    _post(session, report, "versions", token=token)
    _post(session, report, "approve", token=token)
    _post(session, report, "approve", token=token)  # the second one is refused

    assert len(_audit(run_log, report, "report.approved")) == 1
    assert _audit(run_log, report, "override.created") == []
    assert _audit(run_log, report, "version.created") == []


# --- Access, CSRF and origin -------------------------------------------------------------------------------


def test_every_decision_route_needs_a_share_session(run_log: RunLog, app: Any, report: Report) -> None:
    anonymous = TestClient(app, base_url="https://testserver")

    for path in ("overrides", "approve", "versions"):
        response = anonymous.post(f"/reports/{report.run_id}/{path}", data=OVERRIDE_FORM, follow_redirects=False)
        assert response.status_code == 403
    assert _rows(run_log, "report_overrides", report) == []
    assert _rows(run_log, "report_approvals", report) == []


def test_a_cross_origin_post_is_refused(run_log: RunLog, report: Report, session: Session) -> None:
    token = _token(session, report)

    for path, data in (("overrides", OVERRIDE_FORM), ("approve", {}), ("versions", {})):
        response = _post(session, report, path, data, token=token, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
    assert _rows(run_log, "report_overrides", report) == []
    assert _rows(run_log, "report_approvals", report) == []


def test_a_same_origin_post_is_accepted(report: Report, session: Session) -> None:
    response = _post(session, report, "approve", headers={"Origin": "https://testserver"})

    assert response.status_code == 303


@pytest.mark.parametrize("token", [None, "", "0" * 64])
def test_a_missing_or_wrong_csrf_token_is_refused(
    run_log: RunLog, report: Report, session: Session, token: str | None
) -> None:
    data = dict(OVERRIDE_FORM)
    if token is not None:
        data["csrf_token"] = token

    for path in ("overrides", "approve", "versions"):
        response = session.client.post(f"/reports/{report.run_id}/{path}", data=data, follow_redirects=False)
        assert response.status_code == 403
    assert _rows(run_log, "report_overrides", report) == []
    assert _rows(run_log, "report_approvals", report) == []


def test_another_sessions_csrf_token_is_refused(app: Any, run_log: RunLog, report: Report, session: Session) -> None:
    other = _open_session(app, run_log.database_url)
    stolen = _token(other, report)

    response = session.client.post(
        f"/reports/{report.run_id}/approve", data={"csrf_token": stolen}, follow_redirects=False
    )

    assert response.status_code == 403
    assert _rows(run_log, "report_approvals", report) == []


def test_the_bearer_secret_is_not_a_way_in(run_log: RunLog, report: Report, app: Any) -> None:
    anonymous = TestClient(app, base_url="https://testserver")

    response = anonymous.post(
        f"/reports/{report.run_id}/approve",
        data={"csrf_token": "x"},
        headers={"Authorization": "Bearer anything"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert _rows(run_log, "report_approvals", report) == []


# --- Escaping and isolation --------------------------------------------------------------------------------


def test_officer_text_is_escaped_everywhere_it_is_rendered(report: Report, session: Session) -> None:
    assert _override(session, report, text=f"note {SCRIPT} <img src=x onerror=1>").status_code == 303

    for path in ("", "/review", "/print"):
        page = session.client.get(f"/reports/{report.run_id}{path}").text
        assert SCRIPT not in page and "<img src=x" not in page
        assert "&lt;script&gt;" in page


def test_officer_text_never_reaches_the_ai_or_the_stored_analysis(run_log: RunLog, report: Report) -> None:
    ai = MagicMock(spec=AIConnection)
    app = create_app(ai=ai, run_log=run_log, evidence=None)
    session = _open_session(app, run_log.database_url)
    before = run_log.get(report.run_id)
    marker = "officer-only-marker-7731"

    assert _override(session, report, text=marker).status_code == 303
    assert _post(session, report, "approve").status_code == 303

    assert ai.mock_calls == []  # no model call of any kind
    after = run_log.get(report.run_id)
    assert (after.report, after.calls, after.status) == (before.report, before.calls, before.status)
    with psycopg.connect(run_log.database_url) as conn:
        for query in (
            "SELECT count(*) FROM runs WHERE runs::text LIKE %s",
            "SELECT count(*) FROM audit_log WHERE audit_log::text LIKE %s",
            "SELECT count(*) FROM officer_questions WHERE officer_questions::text LIKE %s",
        ):
            assert conn.execute(query, (f"%{marker}%",)).fetchone() == (0,), query


# --- The database itself refuses tampering ------------------------------------------------------------------


@pytest.mark.parametrize("table", ["report_versions", "report_overrides", "report_approvals"])
def test_decision_tables_block_update_delete_and_truncate(
    run_log: RunLog, report: Report, session: Session, table: str
) -> None:
    token = _token(session, report)
    assert _override(session, report, token=token).status_code == 303
    assert _post(session, report, "approve", token=token).status_code == 303
    assert _post(session, report, "versions", token=token).status_code == 303

    statements = (f"UPDATE {table} SET run_id = run_id", f"DELETE FROM {table}", f"TRUNCATE {table}")  # noqa: S608
    for statement in statements:
        with psycopg.connect(run_log.database_url) as conn:
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                conn.execute(statement)
    assert _rows(run_log, table, report) != []


def test_the_database_refuses_an_override_on_an_approved_version_even_without_the_app(
    run_log: RunLog, report: Report, session: Session
) -> None:
    assert _post(session, report, "approve").status_code == 303

    with psycopg.connect(run_log.database_url) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="approved and locked"):
            conn.execute(
                "INSERT INTO report_overrides (override_id, run_id, version_number, finding_key, ai_original, "
                "new_value, reason_code, reason_text) VALUES (%s, %s, 1, 'party:0', '{}', 'x', 'other', 'x')",
                (uuid.uuid4(), report.run_id),
            )


def test_the_database_refuses_a_version_that_does_not_branch_from_an_approved_one(
    run_log: RunLog, report: Report
) -> None:
    with psycopg.connect(run_log.database_url) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="approved version"):
            conn.execute(
                "INSERT INTO report_versions (run_id, version_number, parent_version) VALUES (%s, 2, 1)",
                (report.run_id,),
            )


def test_concurrent_approve_and_override_never_leave_an_override_outside_the_approval(
    app: Any, run_log: RunLog, report: Report
) -> None:
    approver = _open_session(app, run_log.database_url)
    editor = _open_session(app, run_log.database_url)
    approve_token, edit_token = _token(approver, report), _token(editor, report)
    outcomes: dict[str, int] = {}

    def approve() -> None:
        outcomes["approve"] = _post(approver, report, "approve", token=approve_token).status_code

    def override() -> None:
        outcomes["override"] = _override(editor, report, token=edit_token).status_code

    threads = [threading.Thread(target=fn) for fn in (approve, override)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["approve"] == 303 and outcomes["override"] in (303, 409)
    stored = [r for r in _rows(run_log, "report_overrides", report) if r[3] == 1]
    assert len(stored) == (1 if outcomes["override"] == 303 else 0)
    (approved,) = _audit(run_log, report, "report.approved")
    # Whatever order the two requests ran in, the approval fingerprint covers every override stored in version 1.
    assert approved.details["override_count"] == len(stored)


# --- Incidents list -----------------------------------------------------------------------------------------


def test_the_incidents_list_shows_approved_once_a_version_is_approved(
    app: Any, run_log: RunLog, session: Session
) -> None:
    mine = _report()
    _record(run_log, mine, Attribution(session_id=session.session_id, link_id=session.link_id))
    assert [r.status_label for r in run_log.list_runs_for_link(session.link_id)] == ["Ready"]
    assert "Ready" in session.client.get("/incidents").text

    assert _post(session, mine, "approve").status_code == 303

    (row,) = run_log.list_runs_for_link(session.link_id)
    assert row.status_label == "Approved" and row.is_ready is True
    page = session.client.get("/incidents").text
    assert "Approved" in page and "Review workspace" in page


def test_incident_row_labels_approved_only_for_a_valid_succeeded_run() -> None:
    run = Run(
        run_id=uuid.uuid4(),
        origin=Origin.APP,
        created_at=datetime.now(UTC),
        input_hash=HASH,
        report=_report(),
        status=RunStatus.SUCCEEDED,
        total_latency_ms=0,
    )

    assert IncidentRow(run=run, view_count=1).status_label == "Ready"
    assert IncidentRow(run=run, view_count=1, is_approved=True).status_label == "Approved"
    invalid = run.model_copy(update={"report": _report(is_valid=False)})
    assert IncidentRow(run=invalid, view_count=1, is_approved=True).status_label == "Invalid"

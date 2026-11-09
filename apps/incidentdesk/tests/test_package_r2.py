"""MIG-05 Round 2: officer text must not make approved versions unexportable (M1) and related hardening."""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incident_report.audit import canonical_json
from incident_report.domain import Report
from incident_report.runlog import RunLog
from test_package_parity import approve, csrf_token
from test_package_verify import built as built
from test_report_print import workspace as workspace

ALLOWED_TEXT = [
    "Note:\u0627\u0644\u0633\u0627\u0626\u0642 stopped",
    "checked /ref",
    "lane (/left)",
    "Speed limit 60 km/h",
    "Lawful source https://example.com/law/14?article=3",
    "Part 1/2 and/or part 2",
    "12:30",
    "ratio 3:1",
]
REFUSED_TEXT = [
    "C:\\Users\\x",
    "see C:\\Users\\x\\clip.mp4",
    "/home/x/clip.mp4",
    "copied from /Users/alice/evidence",
    "clip at \\\\server\\share\\clip.mp4",
    "file:///tmp/private",
    "http://localhost:8000/private",
    "path:C:" + chr(92) + "Users" + chr(92) + "x",
    "Note:C:/Users/x",
    "{C:/Users/x}",
    "path:/home/x/a",
    "path./home/x/a",
    "/root/evidence",
    "/srv/evidence",
    "/Volumes/evidence",
    "/private/evidence",
    "/var/evidence",
    "~/evidence",
    "Note:/VoLuMeS/evidence",
    "Note:/PrIvAtE/evidence",
    "Note:~/evidence",
]
JSON_PATHS = ["decisions.json", "manifest.json", "reconstruction/import/bundle.json"]


@pytest.mark.parametrize("path", JSON_PATHS)
@pytest.mark.parametrize("text", ALLOWED_TEXT)
def test_ordinary_officer_text_is_allowed_in_json_payloads(path: str, text: str) -> None:
    from incident_report.web.package_safety import validate_payload

    validate_payload(path, canonical_json({"reason": text, "nested": [{"ai_original": text}]}).encode())


def test_ordinary_text_is_allowed_in_jsonl_and_html_payloads() -> None:
    from incident_report.web.package_safety import validate_payload

    line = canonical_json({"note": "Note:\u0627 checked /ref (/left) 60 km/h"})
    validate_payload("reconstruction/import/events.jsonl", (line + "\n" + line + "\n").encode())
    validate_payload("report.html", "<p>Note:\u0627 checked /ref (/left) 60 km/h</p>".encode())


@pytest.mark.parametrize("path", [*JSON_PATHS, "reconstruction/import/events.jsonl"])
@pytest.mark.parametrize("text", REFUSED_TEXT)
def test_real_machine_references_are_refused_and_the_message_names_the_payload(path: str, text: str) -> None:
    from incident_report.web.package_safety import UnsafePayload, validate_payload

    with pytest.raises(UnsafePayload, match="machine|host|path") as refusal:
        validate_payload(path, canonical_json({"reason": text, "n": {"k": ["ok", text]}}).encode())
    assert path in str(refusal.value)


def test_the_configured_evidence_root_is_refused_wherever_it_appears() -> None:
    from incident_report.web.package_safety import UnsafePayload, validate_payload

    root = "/incidentdesk-ev-test"
    content = canonical_json({"reason": f"stored at {root}/objects/ab"}).encode()
    validate_payload("decisions.json", content)  # not a known machine root without configuration
    with pytest.raises(UnsafePayload, match="decisions.json"):
        validate_payload("decisions.json", content, evidence_root=root)
    with pytest.raises(UnsafePayload, match="report.html"):
        validate_payload("report.html", f"<p>{root}/objects</p>".encode(), evidence_root=root)


def test_approved_version_with_arabic_and_slash_officer_text_still_downloads(
    workspace: tuple[TestClient, Report],
) -> None:
    client, report = workspace
    approve(client, report)
    url = f"/reports/{report.run_id}"
    fields = {"csrf_token": csrf_token(client, report)}
    assert client.post(url + "/versions", data=fields, follow_redirects=False).status_code == 303
    reason = "Note:\u0627\u0644\u0633\u0627\u0626\u0642 checked /ref (/left) 60 km/h"
    response = client.post(
        url + "/overrides",
        data={
            **fields,
            "finding_key": "party:0",
            "new_value": "Not at Fault",
            "reason_code": "other",
            "reason_text": reason,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    approve(client, report)
    package = client.get(url + "/versions/2/package")
    assert package.status_code == 200, package.text
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert "\\u0627" in archive.read("decisions.json").decode()


def test_build_refuses_text_that_reveals_the_configured_evidence_root(
    workspace: tuple[TestClient, Report], run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import cast

    from fastapi import FastAPI

    from incident_report.package import PackageRefused, build
    from incident_report.web.package_safety import validate_payload
    from test_review_workspace_r2 import store_report

    client, report = workspace
    evidence = cast(FastAPI, client.app).state.evidence
    # This root intentionally stays outside the generic machine-root list, including
    # the R3 additions. Actual original evidence remains in the fixture's real store.
    root = Path("/incidentdesk-ev-test")
    monkeypatch.setattr(type(evidence), "root", property(lambda _store: root))
    leaked = evidence.root.as_posix() + "/objects/ab"
    validate_payload("report.html", f"<p>Synthetic stored at {leaked}</p>".encode())
    report = store_report(run_log, report.model_copy(update={"summary": "Synthetic stored at " + leaked}))
    approve(client, report)
    with pytest.raises(PackageRefused, match="report.html"):
        build(report.run_id, 1, log=run_log, evidence=evidence)


def _approved_v2(client: TestClient, report: Report) -> str:
    approve(client, report)
    url = f"/reports/{report.run_id}"
    fields = {"csrf_token": csrf_token(client, report)}
    assert client.post(url + "/versions", data=fields, follow_redirects=False).status_code == 303
    assert (
        client.post(
            url + "/overrides",
            data={
                **fields,
                "finding_key": "party:0",
                "new_value": "Not at Fault",
                "reason_code": "other",
                "reason_text": "Synthetic timezone check",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    approve(client, report)
    return url


def test_package_bytes_and_timestamps_do_not_depend_on_the_database_session_timezone(
    workspace: tuple[TestClient, Report], run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from typing import cast

    from fastapi import FastAPI

    from incident_report.package import build

    client, report = workspace
    _approved_v2(client, report)
    evidence = cast(FastAPI, client.app).state.evidence
    packages = {}
    for zone in ("UTC", "Asia/Dubai", "America/Los_Angeles"):
        monkeypatch.setenv("PGTZ", zone)
        packages[zone] = build(report.run_id, 2, log=run_log, evidence=evidence)
    assert packages["UTC"] == packages["Asia/Dubai"] == packages["America/Los_Angeles"]
    with zipfile.ZipFile(io.BytesIO(packages["Asia/Dubai"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        decisions = json.loads(archive.read("decisions.json"))
        html = archive.read("report.html").decode()
    stamps = [
        manifest["approved_at"],
        decisions["approval"]["approved_at"],
        *(item["created_at"] for item in decisions["lineage"]),
        *(item["created_at"] for item in decisions["overrides"].values()),
    ]
    assert len(stamps) >= 4
    assert all(stamp.endswith("+00:00") for stamp in stamps), stamps
    assert manifest["approved_at"] in html


def _page(head: str = "", body: str = "<p>Report</p>") -> bytes:
    return f'<!doctype html><html><head><meta charset="utf-8">{head}</head><body>{body}</body></html>'.encode()


@pytest.mark.parametrize(
    "head",
    [
        "<style>@import url(https://example.org/a.css);</style>",
        "<style>@IMPORT 'https://example.org/a.css';</style>",
        "<style>body{background:url(https://example.org/a.png)}</style>",
        "<style>body{background:URL( 'http://example.org/a.png' )}</style>",
        '<style>body{background:url("data:image/png;base64,AAAA")}</style>',
        "<style>body{background:image-set('https://example.org/a.png' 1x)}</style>",
        "<style>body{color:red}</style><style>@import 'x.css';",  # unclosed second style is still scanned
        '<meta http-equiv="refresh" content="0;url=https://example.org/">',
        '<meta HTTP-EQUIV=" Refresh " content="5">',
        '<link rel="prefetch" href="data:text/css;base64,Ym9keXt9">',
        '<link rel="preload" href="https://example.org/a.css" as="style">',
        '<link rel="dns-prefetch" href="https://example.org/">',
        '<link rel="Preconnect" href="https://example.org/">',
        '<link rel="stylesheet preload" href="data:text/css;base64,Ym9keXt9">',
    ],
)
def test_html_refuses_remaining_active_network_hints(head: str) -> None:
    from incident_report.web.package_safety import UnsafePayload, validate_payload

    with pytest.raises(UnsafePayload, match="report.html"):
        validate_payload("report.html", _page(head))


def test_html_refuses_ping_attribute() -> None:
    from incident_report.web.package_safety import UnsafePayload, validate_payload

    with pytest.raises(UnsafePayload, match="report.html"):
        validate_payload(
            "report.html", _page(body='<a href="https://example.org/law" ping="https://example.org/p">x</a>')
        )


@pytest.mark.parametrize(
    "head",
    [
        "<style>@font-face{font-family:F;src:url(data:font/woff2;base64,AAAA)}body{color:#111}</style>",
        '<meta http-equiv="content-type" content="text/html; charset=utf-8">',
        '<link rel="stylesheet" href="data:text/css;base64,Ym9keXt9">',
    ],
)
def test_html_keeps_inert_styles_and_metadata(head: str) -> None:
    from incident_report.web.package_safety import validate_payload

    validate_payload("report.html", _page(head, '<a href="https://example.org/law">source</a>'))


def _files(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _zip(files: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED, comment: bytes = b"") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.comment = comment
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_verifier_limits_baseline_accepts_a_plain_rewrite(built: bytes) -> None:
    from incident_report.package import verify

    assert verify(_zip(_files(built)))["version_number"] == 1


def test_verifier_refuses_too_many_members(built: bytes) -> None:
    from incident_report.package import verify
    from incident_report.package_format import MAX_PACKAGE_MEMBERS, VerificationRefused

    files = _files(built)
    files.update({f"extra/{n}.txt": b"x" for n in range(MAX_PACKAGE_MEMBERS + 1)})
    with pytest.raises(VerificationRefused, match="members"):
        verify(_zip(files))


def test_verifier_refuses_oversized_declared_total(built: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_report.package import verify
    from incident_report.package_format import VerificationRefused
    from incident_report.web import package_verification

    monkeypatch.setattr(package_verification, "MAX_PACKAGE_BYTES", sum(map(len, _files(built).values())) - 1)
    with pytest.raises(VerificationRefused, match="total size"):
        verify(built)
    monkeypatch.setattr(package_verification, "MAX_PACKAGE_BYTES", sum(map(len, _files(built).values())))
    verify(built)  # exactly at the bound is accepted


def test_verifier_refuses_compressed_members(built: bytes) -> None:
    from incident_report.package import verify
    from incident_report.package_format import VerificationRefused

    with pytest.raises(VerificationRefused, match="stored|compress"):
        verify(_zip(_files(built), compression=zipfile.ZIP_DEFLATED))


def test_verifier_refuses_an_archive_comment(built: bytes) -> None:
    from incident_report.package import verify
    from incident_report.package_format import VerificationRefused

    with pytest.raises(VerificationRefused, match="comment|outside"):
        verify(_zip(_files(built), comment=b"hidden note"))


@pytest.mark.parametrize("where", ["before", "after"])
def test_verifier_refuses_bytes_outside_the_archive(built: bytes, where: str) -> None:
    from incident_report.package import verify
    from incident_report.package_format import VerificationRefused

    padded = b"JUNK" + built if where == "before" else built + b"JUNK"
    with pytest.raises(VerificationRefused, match="outside|before|after|comment"):
        verify(padded)


def test_verifier_cli_reports_the_new_refusals(
    built: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from incident_report.package import main

    path = tmp_path / "padded.zip"
    path.write_bytes(built + b"JUNK")
    assert main(["verify", str(path)]) == 1
    assert "refused" in capsys.readouterr().err.lower()

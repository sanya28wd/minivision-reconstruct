"""MIG-05 E: verify/extract/open references in a clean destination without app settings."""

import io
import os
import re
import subprocess
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from test_package_verify import built as built
from test_report_print import workspace as workspace


class References(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.links: list[tuple[str, str, str]] = []
        self.tags: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for name, value in attrs:
            if name in {"src", "href", "action", "poster", "srcset"}:
                self.links.append((tag, name, value or ""))


def test_package_verifies_and_remains_self_contained_in_clean_folder(built: bytes, tmp_path: Path) -> None:
    from incident_report.package import verify

    clean = tmp_path / "clean-destination"
    clean.mkdir()
    archive_path = clean / "incident.zip"
    archive_path.write_bytes(built)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "deliberately-invalid-no-database-for-verification"
    environment.pop("INCIDENT_EVIDENCE_DIR", None)
    result = subprocess.run(
        [sys.executable, "-m", "incident_report.package", "verify", archive_path.name],
        cwd=clean,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout
    verify(built)  # Refuse malicious members before any extraction.
    with zipfile.ZipFile(io.BytesIO(built)) as archive:
        for name in archive.namelist():
            target = clean / name
            assert target.resolve().is_relative_to(clean.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    report = (clean / "report.html").read_text(encoding="utf-8")
    assert str(tmp_path) not in report
    assert "file://" not in report and "127.0.0.1" not in report and "localhost" not in report
    assert re.search(r"(?<![\w])[A-Za-z]:[\\/]", report) is None
    parsed = References(report)
    assert not {"script", "iframe", "video", "audio", "object", "embed", "form"} & set(parsed.tags)
    citations = []
    for tag, _name, value in parsed.links:
        if tag == "a" and value.startswith(("https://", "http://")):
            citations.append(value)
            continue  # Owner-approved inert legal-source exception, never a fetched resource.
        assert not value.startswith("/") and not value.startswith("file:")
        assert value.startswith(("data:text/css;base64,", "#"))
    assert citations == ["https://example.com/source"]
    assert list((clean / "views").glob("*.mp4"))
    assert "python -m incident_report.package verify <zip>" in (clean / "README.txt").read_text(encoding="utf-8")

"""The dependency guard (RPT-02): every locked package must really exist on PyPI, and every package the
code imports must be declared in pyproject.toml.

Research note AI_ASSISTED_PRODUCTION_ENGINEERING.md section 5 ("slopsquatting"): models invent package
names and attackers register them. Defence: check a new dependency exists, is established and is pinned.
The tests talk to a small local HTTP server that imitates PyPI's JSON API, so they are deterministic and
need no network. (pip-audit, which does need the network, is run by CI and by hand; see docs/CI_GUARDRAILS.md.)
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import check_dependencies
from git_sandbox import GitSandbox

NOW = datetime.now(UTC)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _hash(letter: str) -> str:
    return letter * 64


class FakePyPI:
    """Serves /pypi/<name>/json and /pypi/<name>/<version>/json from a dict, like PyPI's JSON API."""

    def __init__(self) -> None:
        # name -> version -> {"hashes": [...], "uploaded": iso, "yanked": bool}
        self.projects: dict[str, dict[str, dict[str, Any]]] = {}
        self.broken = False
        self.server: HTTPServer | None = None

    def add(self, name: str, version: str, letter: str, days_ago: int = 400, yanked: bool = False) -> None:
        self.projects.setdefault(name, {})[version] = {
            "hashes": [_hash(letter)],
            "uploaded": _iso(days_ago),
            "yanked": yanked,
        }

    def response(self, path: str) -> tuple[int, dict[str, Any] | None]:
        parts = [p for p in path.split("/") if p]  # pypi, name, [version], json
        if self.broken:
            return 500, None
        if len(parts) == 3 and parts[0] == "pypi" and parts[2] == "json" and parts[1] in self.projects:
            versions = self.projects[parts[1]]
            return 200, {
                "info": {"name": parts[1], "project_urls": {"Homepage": f"https://example.invalid/{parts[1]}"}},
                "releases": {
                    v: [{"upload_time_iso_8601": d["uploaded"], "yanked": d["yanked"]}] for v, d in versions.items()
                },
            }
        if len(parts) == 4 and parts[0] == "pypi" and parts[3] == "json":
            entry = self.projects.get(parts[1], {}).get(parts[2])
            if entry is not None:
                return 200, {
                    "info": {"name": parts[1], "version": parts[2], "yanked": entry["yanked"]},
                    "urls": [
                        {
                            "digests": {"sha256": h},
                            "upload_time_iso_8601": entry["uploaded"],
                            "yanked": entry["yanked"],
                        }
                        for h in entry["hashes"]
                    ],
                }
        return 404, None


@pytest.fixture
def pypi() -> Iterator[tuple[FakePyPI, str]]:
    fake = FakePyPI()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = fake.response(self.path)
            payload = json.dumps(body or {"message": "error"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_port}/pypi"
    finally:
        server.shutdown()
        server.server_close()


def lock_text(*packages: tuple[str, str, str], registry: str = "https://pypi.org/simple") -> str:
    """A minimal uv.lock: (name, version, hash letter) per package, plus the project itself."""
    out = ["version = 1", 'requires-python = ">=3.12"', ""]
    out += ["[[package]]", 'name = "demo-project"', 'version = "0.1.0"', 'source = { editable = "." }', ""]
    for name, version, letter in packages:
        out += [
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            f'source = {{ registry = "{registry}" }}',
            f'sdist = {{ url = "https://files.example.invalid/{name}.tar.gz", hash = "sha256:{_hash(letter)}", size = 1 }}',
            "",
        ]
    return "\n".join(out)


PYPROJECT = """\
[project]
name = "demo-project"
version = "0.1.0"
dependencies = ["pydantic>=2"]

[dependency-groups]
dev = ["pytest>=9"]
"""


@pytest.fixture
def project(tmp_path: Path) -> GitSandbox:
    """A git repo holding a demo project whose base commit has an old lock with one package."""
    sandbox = GitSandbox(tmp_path / "project")
    sandbox.write("pyproject.toml", PYPROJECT)
    sandbox.write("uv.lock", lock_text(("old-package", "1.0.0", "a")))
    sandbox.write("src/demo/__init__.py", "import pydantic\n")
    sandbox.write(
        "tests/test_demo.py", "import pytest\nfrom demo import something\n\n\ndef test_it():\n    assert pytest\n"
    )
    sandbox.commit("base")
    return sandbox


def run(project: GitSandbox, index: str, base: str | None, *extra: str) -> int:
    args = ["--repo", str(project.path), "--index-url", index, "--fallback-ref", "main", "--skip-imports", *extra]
    if base:
        args += ["--base", base]
    return check_dependencies.main(args)


def test_every_locked_package_that_exists_with_matching_hashes_passes(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    base = project.head()
    project.branch("feature")

    assert run(project, index, base) == 0

    assert "OK" in capsys.readouterr().out


def test_a_package_that_does_not_exist_on_pypi_fails_and_is_named(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    base = project.head()
    project.branch("feature")
    project.write("uv.lock", lock_text(("old-package", "1.0.0", "a"), ("totally-invented-lib", "0.1.0", "b")))
    project.commit()

    assert run(project, index, base) == 1

    out = capsys.readouterr().out
    assert "totally-invented-lib" in out
    assert "does not exist" in out


def test_a_version_pypi_has_never_published_fails(project: GitSandbox, pypi: tuple[FakePyPI, str]) -> None:
    fake, index = pypi
    fake.add("old-package", "2.0.0", "a")  # the lock pins 1.0.0, which was never published

    assert run(project, index, project.head()) == 1


def test_a_locked_hash_that_pypi_does_not_list_fails(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "f")  # PyPI's file has a different hash from the lock's

    assert run(project, index, project.head()) == 1

    assert "hash" in capsys.readouterr().out


def test_a_yanked_release_fails(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a", yanked=True)

    assert run(project, index, project.head()) == 1

    assert "yanked" in capsys.readouterr().out


def test_a_package_from_somewhere_other_than_pypi_fails(project: GitSandbox, pypi: tuple[FakePyPI, str]) -> None:
    _, index = pypi
    project.write("uv.lock", lock_text(("old-package", "1.0.0", "a"), registry="https://evil.example.invalid/simple"))
    project.commit()

    assert run(project, index, None) == 1


def test_a_brand_new_project_added_by_this_change_fails_the_age_floor(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    fake.add("freshly-registered", "0.0.1", "b", days_ago=3)
    base = project.head()
    project.branch("feature")
    project.write("uv.lock", lock_text(("old-package", "1.0.0", "a"), ("freshly-registered", "0.0.1", "b")))
    project.commit()

    assert run(project, index, base) == 1

    out = capsys.readouterr().out
    assert "freshly-registered" in out
    assert "days old" in out


def test_a_well_established_new_dependency_passes_and_its_facts_are_printed_for_the_reviewer(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    fake.add("established", "2.0.0", "b", days_ago=900)
    base = project.head()
    project.branch("feature")
    project.write("uv.lock", lock_text(("old-package", "1.0.0", "a"), ("established", "2.0.0", "b")))
    project.commit()

    assert run(project, index, base) == 0

    out = capsys.readouterr().out
    assert "NEW" in out
    assert "established==2.0.0" in out


def test_the_age_floor_only_applies_to_packages_that_are_new_in_this_change(
    project: GitSandbox, pypi: tuple[FakePyPI, str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a", days_ago=2)  # young, but already in the base lock
    base = project.head()
    project.branch("feature")

    assert run(project, index, base) == 0


def test_a_new_version_of_a_package_already_locked_is_not_a_new_package(
    project: GitSandbox, pypi: tuple[FakePyPI, str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.1", "c", days_ago=1)
    base = project.head()
    project.branch("feature")
    project.write("uv.lock", lock_text(("old-package", "1.0.1", "c")))
    project.commit()

    assert run(project, index, base) == 0


def test_the_age_floor_can_be_changed_on_the_command_line(project: GitSandbox, pypi: tuple[FakePyPI, str]) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    fake.add("youngish", "1.0.0", "b", days_ago=10)
    base = project.head()
    project.branch("feature")
    project.write("uv.lock", lock_text(("old-package", "1.0.0", "a"), ("youngish", "1.0.0", "b")))
    project.commit()

    assert run(project, index, base) == 1
    assert run(project, index, base, "--min-age-days", "5") == 0


def test_first_push_of_a_new_branch_compares_the_lock_with_main(
    project: GitSandbox, pypi: tuple[FakePyPI, str]
) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    fake.add("freshly-registered", "0.0.1", "b", days_ago=3)
    project.branch("feature")
    project.write("uv.lock", lock_text(("old-package", "1.0.0", "a"), ("freshly-registered", "0.0.1", "b")))
    project.commit()

    assert run(project, index, "0" * 40) == 1


def test_when_pypi_cannot_be_reached_the_check_errors_instead_of_passing(
    project: GitSandbox, pypi: tuple[FakePyPI, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, index = pypi
    fake.broken = True

    assert run(project, index, project.head()) == 2

    assert "could not" in capsys.readouterr().err.lower()


def test_the_project_itself_is_not_looked_up_on_pypi(project: GitSandbox, pypi: tuple[FakePyPI, str]) -> None:
    fake, index = pypi
    fake.add("old-package", "1.0.0", "a")
    # "demo-project" is in the lock as an editable path package and is not in the fake index.

    assert run(project, index, project.head()) == 0


# --- undeclared imports -------------------------------------------------------------------------------


def run_imports(project: GitSandbox) -> int:
    return check_dependencies.main(["--repo", str(project.path), "--skip-index"])


def test_code_that_imports_only_declared_packages_passes(project: GitSandbox) -> None:
    assert run_imports(project) == 0


def test_importing_a_package_that_is_only_installed_transitively_fails(
    project: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    # This is RPT-01 review finding 3: pydantic imported directly but declared only through FastAPI.
    project.write("pyproject.toml", PYPROJECT.replace('["pydantic>=2"]', "[]"))
    project.commit()

    assert run_imports(project) == 1

    out = capsys.readouterr().out
    assert "pydantic" in out
    assert "src/demo/__init__.py" in out


def test_a_package_declared_only_for_development_cannot_be_used_by_the_application(
    project: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write("src/demo/more.py", "import pytest\n")
    project.commit()

    assert run_imports(project) == 1

    assert "development" in capsys.readouterr().out


def test_tests_may_use_development_packages(project: GitSandbox) -> None:
    project.write(
        "tests/test_more.py", "import pytest\nimport pydantic\n\n\ndef test_x():\n    assert pytest and pydantic\n"
    )
    project.commit()

    assert run_imports(project) == 0


def test_an_import_that_no_installed_package_provides_fails(
    project: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write("src/demo/hallucinated.py", "import fastjsonvalidatorx\n")
    project.commit()

    assert run_imports(project) == 1

    assert "fastjsonvalidatorx" in capsys.readouterr().out


def test_the_standard_library_and_the_projects_own_modules_are_not_dependencies(project: GitSandbox) -> None:
    project.write(
        "src/demo/own.py",
        "import json\nimport demo\nfrom demo import own\nfrom . import something\nfrom __future__ import annotations\n",
    )
    project.commit()

    assert run_imports(project) == 0


def test_import_names_that_differ_from_the_package_name_are_matched_through_package_metadata(
    project: GitSandbox,
) -> None:
    # `import multipart` is provided by the distribution `python-multipart`.
    project.write("pyproject.toml", PYPROJECT.replace('["pydantic>=2"]', '["pydantic>=2", "python-multipart>=0.0.9"]'))
    project.write("src/demo/upload.py", "import multipart\n")
    project.commit()

    assert run_imports(project) == 0


def test_this_repository_declares_every_package_it_imports() -> None:
    # The standing check for RPT-01 finding 3, run against the real project.
    root = Path(__file__).resolve().parent.parent

    assert check_dependencies.main(["--repo", str(root), "--skip-index"]) == 0

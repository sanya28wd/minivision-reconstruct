"""Worker paths have a separate gate; app edits retain their normal protection."""

from pathlib import Path

import pytest

from guard_protected_paths import is_protected, is_watched


def test_worker_scripts_are_watched() -> None:
    assert not is_protected("scripts/process_case.py")
    assert is_watched("scripts/process_case.py")
    assert not is_watched("apps/incidentdesk/scripts/check_all.sh")


@pytest.mark.parametrize("path", ["apps/incidentdesk/src/incident_report/db.py", "apps/incidentdesk/tests/test_web.py"])
def test_app_runtime_and_tests_are_not_protected(path: str) -> None:
    assert not is_protected(path)


@pytest.mark.parametrize("path,expected", [("scripts/process_case.py", 1), ("apps/incidentdesk/src/app.py", 0)])
def test_collaborator_path_gate(tmp_path: Path, path: str, expected: int) -> None:
    from collaborator_paths import check
    from git_sandbox import GitSandbox

    repo = GitSandbox(tmp_path / "repo")
    repo.commit()
    base = repo.head()
    repo.write(path, "change")
    repo.commit()
    assert check(repo.path, base, "HEAD") == expected

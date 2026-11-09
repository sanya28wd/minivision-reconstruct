"""Only owner-approved, nonempty ranges can be the default gate base."""

from pathlib import Path

import pytest

from gate_base import validate
from git_sandbox import GitSandbox
from guardlib import GuardError


@pytest.mark.parametrize("state", ["missing", "lightweight", "nonancestor", "empty"])
def test_default_base_refusals(tmp_path: Path, state: str) -> None:
    repo = GitSandbox(tmp_path / "repo")
    repo.write("note", "base")
    repo.commit()
    tag = "incidentdesk-import-approved"
    if state == "lightweight":
        repo.git("tag", tag)
    elif state in {"nonancestor", "empty"}:
        if state == "nonancestor":
            repo.branch("other")
            repo.write("other", "other")
            repo.commit()
        repo.git("-c", "tag.gpgsign=false", "tag", "-a", tag, "-m", "sandbox approval")
        if state == "nonancestor":
            repo.checkout("main")
    if state != "empty":
        repo.write("ticket", "change")
        repo.commit()
    with pytest.raises(GuardError):
        validate(repo.path, tag, "HEAD")


def test_explicit_base_and_empty_range(tmp_path: Path) -> None:
    repo = GitSandbox(tmp_path / "repo")
    repo.commit()
    base = repo.head()
    with pytest.raises(GuardError):
        validate(repo.path, base, "HEAD")
    repo.write("ticket", "change")
    repo.commit()
    validate(repo.path, base, "HEAD")


def test_annotated_ancestor_base_passes(tmp_path: Path) -> None:
    repo = GitSandbox(tmp_path / "repo")
    repo.commit()
    repo.git("-c", "tag.gpgsign=false", "tag", "-a", "incidentdesk-import-approved", "-m", "sandbox approval")
    repo.write("ticket", "change")
    repo.commit()
    validate(repo.path, "incidentdesk-import-approved", "HEAD")

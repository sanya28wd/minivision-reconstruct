"""Only local commit-msg trailers can approve truth; range checks require the real label."""

from pathlib import Path

import pytest

import guard_protected_paths
from git_sandbox import GitSandbox


@pytest.mark.parametrize(
    "path,reason,ci,expected",
    [
        ("dataset/truth/a.json", "reviewed correction", False, 0),
        ("docs/adr/a.md", "reviewed correction", False, 1),
        ("dataset/truth/a.json", "   ", False, 1),
        ("dataset/truth/a.json", None, False, 1),
        ("dataset/truth/a.json", "reviewed correction", True, 1),
    ],
)
@pytest.mark.parametrize("staged", [False, True])
def test_owner_trailer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    reason: str | None,
    ci: bool,
    expected: int,
    staged: bool,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    if ci:
        monkeypatch.setenv("CI", "true")
    repo = GitSandbox(tmp_path / "repo")
    repo.write("README.md", "base\n")
    base = repo.commit()
    repo.write(path, "{}\n")
    message = "correction\n" + (f"\nOwner-Approved: {reason}\n" if reason is not None else "")
    args = ["--repo", str(repo.path), "--base", base]
    if staged:
        repo.git("add", "-A")
        msg = tmp_path / "message"
        msg.write_text(message, encoding="utf-8")
        args += ["--staged", "--commit-msg-file", str(msg)]
    else:
        repo.commit(message)
    assert guard_protected_paths.main(args) == (expected if staged else 1)


def test_range_ignores_supplied_message_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    repo = GitSandbox(tmp_path / "repo")
    repo.write("README.md", "base\n")
    base = repo.commit()
    repo.write("dataset/truth/a.json", "{}\n")
    repo.commit("correction")
    msg = tmp_path / "message"
    msg.write_text("Owner-Approved: reviewed correction\n", encoding="utf-8")
    args = ["--repo", str(repo.path), "--base", base, "--commit-msg-file", str(msg)]
    assert guard_protected_paths.main(args) == 1
    assert guard_protected_paths.main([*args, "--approved"]) == 0

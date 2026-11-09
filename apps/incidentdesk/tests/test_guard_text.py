"""Staged bytes, not working-copy replacements, determine text guard results."""

import importlib
from pathlib import Path

import pytest

from git_sandbox import GitSandbox


@pytest.mark.parametrize(
    "module,bad,diagnostic",
    [
        ("guard_conflict_markers", b"ok\n<<<<<<< branch\n", "note.md:2"),
        ("guard_utf8", b"ok\n\x97\n", "byte offset 3"),
    ],
)
def test_staged_text_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], module: str, bad: bytes, diagnostic: str
) -> None:
    guard = importlib.import_module(module)
    repo = GitSandbox(tmp_path / "repo")
    repo.write("note.md", "clean\n")
    repo.commit("base")
    repo.write("note.md", "clean staged text\n")
    repo.git("add", "note.md")
    assert guard.main(["--repo", str(repo.path), "--staged"]) == 0
    (repo.path / "note.md").write_bytes(bad)
    repo.git("add", "note.md")
    repo.write("note.md", "unstaged clean\n")
    assert guard.main(["--repo", str(repo.path), "--staged"]) == 1
    assert diagnostic in capsys.readouterr().out
    (repo.path / "note.md").write_bytes(b"\x00" + bad)
    repo.git("add", "note.md")
    assert guard.main(["--repo", str(repo.path), "--staged"]) == 0


@pytest.mark.parametrize("marker", [b"<<<<<<< branch", b"=======", b">>>>>>> branch"])
def test_conflict_guard_exemptions_and_range(tmp_path: Path, marker: bytes) -> None:
    import guard_conflict_markers

    repo = GitSandbox(tmp_path / "repo")
    repo.write("note.md", "base\n")
    base = repo.commit()
    for path in ("uv.lock", "package-lock.json", "docs/reference/a.md"):
        target = repo.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(marker + b"\n")
    repo.commit()
    assert guard_conflict_markers.main(["--repo", str(repo.path), "--base", base]) == 0
    (repo.path / "note.md").write_bytes(marker + b"\n")
    repo.commit()
    assert guard_conflict_markers.main(["--repo", str(repo.path), "--base", base]) == 1

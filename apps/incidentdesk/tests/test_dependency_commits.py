"""Dependency range checks must inspect committed component blobs."""

from pathlib import Path

import check_dependencies
from git_sandbox import GitSandbox


def test_committed_component_lock_controls_offline_gate(tmp_path: Path) -> None:
    repo = GitSandbox(tmp_path / "repo")
    lock = "version = 1\n"
    repo.write("uv.lock", "root lock is not the app lock\n")
    repo.write("apps/incidentdesk/uv.lock", lock)
    (repo.path / "apps/incidentdesk/uv.lock").write_bytes(lock.encode())
    base = repo.commit()
    app = repo.path / "apps/incidentdesk"
    repo.write("note", "ticket")
    repo.commit()
    args = ["--repo", str(app), "--base", base, "--skip-imports", "--frozen-import"]
    assert check_dependencies.main(args) == 0
    repo.write("apps/incidentdesk/uv.lock", lock + "revision = 1\n")
    repo.commit()
    (app / "uv.lock").write_text(lock, encoding="utf-8")
    assert check_dependencies.main(args) == 2
    repo.git("config", "core.autocrlf", "false")
    (app / "uv.lock").write_bytes(lock.replace("\n", "\r\n").encode())
    repo.commit()
    (app / "uv.lock").write_bytes(lock.encode())
    assert check_dependencies.main(args) == 2  # Committed line-ending changes count too.


def test_default_unchanged_real_lock_needs_no_index(tmp_path: Path) -> None:
    from unittest.mock import patch

    repo = GitSandbox(tmp_path / "repo")
    repo.write("apps/incidentdesk/uv.lock", (Path(__file__).parents[1] / "uv.lock").read_text(encoding="utf-8"))
    base = repo.commit()
    repo.write("ticket", "change")
    repo.commit()
    with patch.object(check_dependencies, "Index", side_effect=AssertionError("unexpected index audit")):
        assert (
            check_dependencies.main(
                [
                    "--repo",
                    str(repo.path / "apps/incidentdesk"),
                    "--base",
                    base,
                    "--skip-imports",
                ]
            )
            == 0
        )

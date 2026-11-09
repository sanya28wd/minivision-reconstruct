"""The secret-scanning guard (RPT-02): catches keys and tokens in added lines, in CI and as a pre-commit hook.

Fake keys are built by string concatenation at run time and written only into temporary repositories, so
this file (and the real repository) never contains a string a scanner would take for a key.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import guard_secrets
from git_sandbox import ZEROS, GitSandbox, build_diverged_history

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_secrets.py"

# Shapes of real credentials, assembled from pieces. None of these is a working key.
FAKE_KEYS = {
    "aws": "AKIA" + "QYLPMN5HFIQR7ABC" + "DE",
    "github": "ghp_" + "R2d9xK3mQ7vL1nB8cT5yU0zW4eH6jA2sD9fG",
    "slack": "xoxb-" + "123456789012-" + "1234567890123-" + "abcdefghijklmnopqrstuvwx",
    "stripe": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    # The Gemini / Google API key shape (the project's own provider): "AIza" + 35 characters.
    "google": "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}


@pytest.fixture
def repo(tmp_path: Path) -> GitSandbox:
    sandbox = GitSandbox(tmp_path / "repo")
    sandbox.write("README.md", "hello\n")
    sandbox.write("src/config.py", "DATABASE_URL = 'postgresql:///incident_dev'\n")
    sandbox.commit("base")
    return sandbox


def run_guard(repo: GitSandbox, base: str, *extra: str) -> int:
    return guard_secrets.main(["--repo", str(repo.path), "--base", base, "--fallback-ref", "main", *extra])


@pytest.mark.parametrize("kind", sorted(FAKE_KEYS))
def test_a_planted_key_in_a_new_commit_fails_and_is_not_echoed(
    kind: str, repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write("src/settings.py", f'# settings\nAPI_KEY = "{FAKE_KEYS[kind]}"\n')
    repo.commit()

    assert run_guard(repo, base) == 1

    captured = capsys.readouterr()
    assert "src/settings.py:2" in captured.out
    assert FAKE_KEYS[kind] not in captured.out + captured.err  # never copy a secret into the CI log


def test_a_key_added_and_removed_again_inside_the_same_change_is_still_caught(repo: GitSandbox) -> None:
    # Once pushed, the key is in history even if a later commit deletes it.
    base = repo.head()
    repo.branch("feature")
    repo.write("src/oops.py", f'TOKEN = "{FAKE_KEYS["github"]}"\n')
    repo.commit("oops")
    repo.delete("src/oops.py")
    repo.commit("remove it")

    assert run_guard(repo, base) == 1


def test_a_key_that_was_already_in_the_base_is_not_reported_again(repo: GitSandbox) -> None:
    repo.write("src/old.py", f'TOKEN = "{FAKE_KEYS["aws"]}"\n')
    repo.commit("old mistake")
    base = repo.head()
    repo.branch("feature")
    repo.write("src/new.py", "x = 1\n")
    repo.commit()

    assert run_guard(repo, base) == 0


def test_a_clean_change_passes(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write("src/config.py", "DATABASE_URL = 'postgresql://postgres@localhost:5432/postgres'\n")
    repo.write("docs/notes.md", "sha256 of abc: ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n")
    repo.commit()

    assert run_guard(repo, base) == 0

    assert "OK" in capsys.readouterr().out


def test_removing_a_line_that_held_a_key_is_not_a_finding(repo: GitSandbox) -> None:
    repo.write("src/old.py", f'TOKEN = "{FAKE_KEYS["aws"]}"\n')
    repo.commit("old mistake")
    base = repo.head()
    repo.branch("feature")
    repo.delete("src/old.py")
    repo.commit("clean up")

    assert run_guard(repo, base) == 0


def test_first_push_of_a_new_branch_scans_what_it_adds(repo: GitSandbox) -> None:
    repo.branch("feature")
    repo.write("src/settings.py", f'KEY = "{FAKE_KEYS["google"]}"\n')
    repo.commit()

    assert run_guard(repo, ZEROS) == 1


def test_a_repository_with_no_base_and_no_main_is_scanned_in_full(tmp_path: Path) -> None:
    lone = GitSandbox(tmp_path / "lone")
    lone.write("settings.py", f'KEY = "{FAKE_KEYS["slack"]}"\n')
    lone.commit("initial")

    assert guard_secrets.main(["--repo", str(lone.path), "--base", ZEROS, "--fallback-ref", ""]) == 1


def test_there_is_no_inline_way_to_silence_the_scanner(repo: GitSandbox) -> None:
    # detect-secrets' "pragma: allowlist secret" is deliberately not honoured: an agent could add it too.
    base = repo.head()
    repo.branch("feature")
    repo.write("src/settings.py", f'KEY = "{FAKE_KEYS["aws"]}"  # pragma: allowlist secret\n')
    repo.commit()

    assert run_guard(repo, base) == 1


def test_binary_files_are_skipped_without_crashing(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02" + FAKE_KEYS["aws"].encode() + b"\xff\xfe")
    repo.commit()

    assert run_guard(repo, base) == 0


def test_staged_mode_checks_what_is_about_to_be_committed(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    # This is what the pre-commit hook runs.
    repo.write("src/settings.py", f'KEY = "{FAKE_KEYS["github"]}"\n')
    repo.git("add", "src/settings.py")

    assert guard_secrets.main(["--repo", str(repo.path), "--staged"]) == 1

    assert "src/settings.py:1" in capsys.readouterr().out


def test_staged_mode_ignores_changes_that_are_not_staged(repo: GitSandbox) -> None:
    repo.write("src/settings.py", f'KEY = "{FAKE_KEYS["github"]}"\n')  # written but not `git add`ed

    assert guard_secrets.main(["--repo", str(repo.path), "--staged"]) == 0


def test_staged_mode_passes_when_nothing_secret_is_staged(repo: GitSandbox) -> None:
    repo.write("src/settings.py", "KEY = os.environ['KEY']\n")
    repo.git("add", "src/settings.py")

    assert guard_secrets.main(["--repo", str(repo.path), "--staged"]) == 0


def test_a_head_that_does_not_exist_is_a_plain_error(repo: GitSandbox) -> None:
    assert guard_secrets.main(["--repo", str(repo.path), "--base", "main", "--head", "nope"]) == 2


def test_the_script_runs_on_its_own_the_way_ci_and_the_hook_run_it(repo: GitSandbox) -> None:
    repo.write("src/settings.py", f'KEY = "{FAKE_KEYS["aws"]}"\n')
    repo.git("add", "src/settings.py")

    result = subprocess.run([sys.executable, str(SCRIPT), "--staged"], cwd=repo.path, capture_output=True, text=True)

    assert result.returncode == 1
    assert "src/settings.py:1" in result.stdout
    assert FAKE_KEYS["aws"] not in result.stdout + result.stderr


def test_first_push_of_a_ticket_branch_does_not_rescan_the_integration_branchs_history(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path / "diverged")  # fresh-start already holds a (fake) token in src/leaked.py
    build_diverged_history(sandbox)
    repo = str(sandbox.path)

    both = ["--fallback-ref", "main", "--fallback-ref", "fresh-start"]
    assert guard_secrets.main(["--repo", repo, "--base", ZEROS, *both]) == 0
    assert guard_secrets.main(["--repo", repo, "--base", ZEROS, "--fallback-ref", "main"]) == 1
    assert guard_secrets.main(["--repo", repo, "--base", ZEROS, "--fallback-ref", "nope"]) == 1

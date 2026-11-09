"""The protected-path guard (RPT-02): fails changes to truth, archive, ADRs, reference docs, GLOSSARY.md.

Each test builds a real git repository in a temp directory, makes commits, and runs the guard the way
CI does: `guard_protected_paths.py --base <ref> --head <ref>`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import guard_protected_paths
from git_sandbox import ZEROS, GitSandbox, build_diverged_history

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_protected_paths.py"


@pytest.fixture
def repo(tmp_path: Path) -> GitSandbox:
    sandbox = GitSandbox(tmp_path / "repo")
    sandbox.write("README.md", "hello\n")
    sandbox.write("dataset/truth/clip1.json", '{"a": 1}\n')
    sandbox.write("dataset/archive/old.txt", "old\n")
    sandbox.write("docs/adr/0001-x.md", "adr\n")
    sandbox.write("docs/reference/ref.md", "ref\n")
    sandbox.write("GLOSSARY.md", "terms\n")
    sandbox.write("src/app.py", "x = 1\n")
    sandbox.commit("base")
    return sandbox


def run_guard(repo: GitSandbox, base: str, *extra: str) -> int:
    return guard_protected_paths.main(["--repo", str(repo.path), "--base", base, "--fallback-ref", "main", *extra])


@pytest.mark.parametrize(
    "path",
    [
        "dataset/truth/clip1.json",
        "dataset/truth/new_clip.json",
        "dataset/archive/old.txt",
        "docs/adr/0001-x.md",
        "docs/adr/0099-new.md",
        "docs/reference/ref.md",
        "docs/reference/sub/deep.md",
        "GLOSSARY.md",
    ],
)
def test_editing_or_adding_a_protected_path_fails(
    repo: GitSandbox, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write(path, "changed by an agent\n")
    repo.commit()

    assert run_guard(repo, base) == 1

    assert path in capsys.readouterr().out


def test_deleting_a_protected_file_fails(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.delete("dataset/truth/clip1.json")
    repo.commit()

    assert run_guard(repo, base) == 1


def test_moving_a_file_out_of_a_protected_folder_fails(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.move("dataset/truth/clip1.json", "src/stolen.json")
    repo.commit()

    assert run_guard(repo, base) == 1

    assert "dataset/truth/clip1.json" in capsys.readouterr().out


def test_moving_a_file_into_a_protected_folder_fails(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.move("src/app.py", "docs/adr/app.py")
    repo.commit()

    assert run_guard(repo, base) == 1


def test_a_change_that_touches_nothing_protected_passes(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write("src/app.py", "x = 2\n")
    repo.write("docs/notes.md", "notes\n")
    repo.commit()

    assert run_guard(repo, base) == 0

    assert "OK" in capsys.readouterr().out


@pytest.mark.parametrize(
    "path",
    [
        "dataset/truthy.txt",  # shares a prefix with dataset/truth but is a different path
        "dataset/archive_old.txt",
        "docs/adr-notes.md",
        "docs/references.md",
        "docs/GLOSSARY.md",  # only the root GLOSSARY.md is protected
        "GLOSSARY.md.bak",
        "dataset/videos/clip.json",
    ],
)
def test_similar_looking_paths_are_not_protected(repo: GitSandbox, path: str) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write(path, "fine\n")
    repo.commit()

    assert run_guard(repo, base) == 0


def test_path_matching_ignores_letter_case(repo: GitSandbox) -> None:
    # Windows and macOS file systems are case-insensitive, so Dataset/Truth is the same folder there.
    base = repo.head()
    repo.branch("feature")
    repo.write("Dataset/Truth/sneaky.json", "{}\n")
    repo.write("glossary.md", "sneaky\n")
    repo.commit()

    assert run_guard(repo, base) == 1


def test_the_owner_approval_flag_lets_a_protected_change_through_but_still_lists_it(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write("docs/adr/0001-x.md", "approved edit\n")
    repo.commit()

    assert run_guard(repo, base, "--approved") == 0

    out = capsys.readouterr().out
    assert "docs/adr/0001-x.md" in out
    assert "approved" in out.lower()


def test_changes_that_landed_on_main_after_the_branch_point_are_not_blamed_on_the_branch(
    repo: GitSandbox,
) -> None:
    repo.branch("feature")
    repo.write("src/app.py", "x = 3\n")
    repo.commit()
    repo.checkout("main")
    repo.write("GLOSSARY.md", "someone approved this on main\n")
    repo.commit()
    repo.checkout("feature")

    assert run_guard(repo, "main") == 0


def test_first_push_of_a_new_branch_compares_against_main(repo: GitSandbox) -> None:
    # GitHub sends an all-zero "before" SHA when a branch is pushed for the first time.
    repo.branch("feature")
    repo.write("dataset/truth/clip1.json", '{"a": 2}\n')
    repo.commit()

    assert run_guard(repo, ZEROS) == 1


def test_first_push_of_a_clean_new_branch_passes(repo: GitSandbox) -> None:
    repo.branch("feature")
    repo.write("src/app.py", "x = 9\n")
    repo.commit()

    assert run_guard(repo, ZEROS) == 0


def test_a_before_sha_that_no_longer_exists_after_a_force_push_falls_back_to_main(
    repo: GitSandbox,
) -> None:
    repo.branch("feature")
    repo.write("docs/reference/ref.md", "changed\n")
    repo.commit()

    assert run_guard(repo, "1234567890abcdef1234567890abcdef12345678") == 1


def test_with_no_base_and_no_main_everything_counts_as_new_so_protected_files_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lone = GitSandbox(tmp_path / "lone")
    lone.write("GLOSSARY.md", "first commit\n")
    lone.commit("initial")

    code = guard_protected_paths.main(["--repo", str(lone.path), "--base", ZEROS, "--fallback-ref", "nope"])

    assert code == 1
    assert "GLOSSARY.md" in capsys.readouterr().out


def test_a_huge_list_of_protected_paths_is_summarised_but_still_fails(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.head()
    repo.branch("feature")
    for i in range(45):
        repo.write(f"dataset/truth/clip{i:02d}.json", "{}\n")
    repo.commit()

    assert run_guard(repo, base) == 1

    out = capsys.readouterr().out
    assert "and 15 more" in out
    assert "45 protected path(s)" in out


def test_a_head_that_does_not_exist_is_a_plain_error_not_a_pass(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    code = guard_protected_paths.main(["--repo", str(repo.path), "--base", "main", "--head", "no-such-ref"])

    assert code == 2
    assert "no-such-ref" in capsys.readouterr().err


def test_guardrail_files_are_reported_as_a_notice_but_do_not_fail(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write(".github/workflows/ci.yml", "name: x\n")
    repo.write("scripts/guard_x.py", "pass\n")
    repo.commit()

    assert run_guard(repo, base) == 0

    out = capsys.readouterr().out
    assert "NOTICE" in out
    assert ".github/workflows/ci.yml" in out


def test_the_script_runs_on_its_own_the_way_ci_runs_it(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write("dataset/truth/clip1.json", "{}\n")
    repo.commit()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", "HEAD"],
        cwd=repo.path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "dataset/truth/clip1.json" in result.stdout


# --- first push of a ticket branch cut from an integration branch that diverged from main -------------


def test_first_push_compares_with_the_nearest_of_several_fallback_refs(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path / "diverged")
    build_diverged_history(sandbox)

    code = guard_protected_paths.main(
        ["--repo", str(sandbox.path), "--base", ZEROS, "--fallback-ref", "main", "--fallback-ref", "fresh-start"]
    )

    assert code == 0


def test_first_push_with_only_the_old_ref_blames_the_branch_for_history_it_did_not_write(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path / "diverged")
    build_diverged_history(sandbox)

    assert guard_protected_paths.main(["--repo", str(sandbox.path), "--base", ZEROS, "--fallback-ref", "main"]) == 1


def test_first_push_with_no_usable_fallback_still_fails_closed(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path / "diverged")
    build_diverged_history(sandbox)

    assert guard_protected_paths.main(["--repo", str(sandbox.path), "--base", ZEROS, "--fallback-ref", "nope"]) == 1
    assert guard_protected_paths.main(["--repo", str(sandbox.path), "--base", ZEROS, "--fallback-ref", ""]) == 1


def test_fallback_refs_may_be_one_comma_or_space_separated_value_and_bare_names_find_origin_refs(
    tmp_path: Path,
) -> None:
    # CI passes "<default branch> <INTEGRATION_BRANCHES>" as a single value, with bare branch names.
    sandbox = GitSandbox(tmp_path / "diverged")
    build_diverged_history(sandbox)
    clone = GitSandbox.clone(sandbox, tmp_path / "clone")  # in the clone the branches exist only as origin/*
    clone.git("checkout", "-q", "--detach", "origin/feature")

    for value in ("main fresh-start", "main,fresh-start", "origin/main, origin/fresh-start"):
        code = guard_protected_paths.main(["--repo", str(clone.path), "--base", ZEROS, "--fallback-ref", value])
        assert code == 0, value


def test_a_candidate_that_already_contains_the_pushed_commit_does_not_make_the_comparison_empty(
    tmp_path: Path,
) -> None:
    # First push of an integration branch itself: fresh-start contains HEAD, main is far. The nearest is
    # fresh-start at distance 0, so the guard must look at HEAD's own change (here: the flush commit).
    sandbox = GitSandbox(tmp_path / "diverged")
    build_diverged_history(sandbox)
    sandbox.checkout("fresh-start")

    code = guard_protected_paths.main(
        ["--repo", str(sandbox.path), "--base", ZEROS, "--fallback-ref", "main", "--fallback-ref", "fresh-start"]
    )

    assert code == 1  # HEAD itself is the flush commit, which edits dataset/truth: that is its own change

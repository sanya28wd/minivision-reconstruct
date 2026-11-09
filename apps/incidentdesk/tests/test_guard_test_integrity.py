"""The test-integrity guard (RPT-02): flags deleted, skipped, xfail-marked or loosened tests.

Spec 001 "Testing Decisions": tests may not be deleted, skipped or weakened without owner approval.
Each case builds a real git repository whose base commit has a small test file, commits a change on a
branch, and runs the guard the way CI does. The "bad" test sources below are plain strings; they are
parsed by the guard, never run, so this file itself contains no skip or xfail marker.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import guard_test_integrity
from git_sandbox import ZEROS, GitSandbox, build_diverged_history

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_test_integrity.py"

BASE_TESTS = textwrap.dedent(
    """\
    import pytest


    def test_adds():
        assert 1 + 1 == 2
        assert 2 + 2 == 4


    def test_raises():
        with pytest.raises(ValueError):
            int("x")


    class TestThing:
        def test_method(self):
            assert "a".upper() == "A"
    """
)

TEST_PATH = "tests/test_sample.py"


@pytest.fixture
def repo(tmp_path: Path) -> GitSandbox:
    sandbox = GitSandbox(tmp_path / "repo")
    sandbox.write(TEST_PATH, BASE_TESTS)
    sandbox.write("src/app.py", "def add(a, b):\n    return a + b\n")
    sandbox.write(
        "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra --strict-markers"\n'
    )
    sandbox.commit("base")
    return sandbox


def run_guard(repo: GitSandbox, base: str, *extra: str) -> int:
    return guard_test_integrity.main(["--repo", str(repo.path), "--base", base, "--fallback-ref", "main", *extra])


def edit_and_run(repo: GitSandbox, new_text: str, path: str = TEST_PATH, *extra: str) -> int:
    base = repo.head()
    repo.branch("feature")
    repo.write(path, new_text)
    repo.commit()
    return run_guard(repo, base, *extra)


# --- deleted tests ---------------------------------------------------------------------------------


def test_deleting_a_test_file_fails(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.delete(TEST_PATH)
    repo.commit()

    assert run_guard(repo, base) == 1

    out = capsys.readouterr().out
    assert TEST_PATH in out
    assert "deleted" in out


def test_deleting_one_test_function_fails(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    new = BASE_TESTS.replace('def test_raises():\n    with pytest.raises(ValueError):\n        int("x")\n', "")

    assert edit_and_run(repo, new) == 1

    assert "test_raises" in capsys.readouterr().out


def test_deleting_a_test_method_inside_a_class_fails(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    new = BASE_TESTS.replace('    def test_method(self):\n        assert "a".upper() == "A"\n', "    pass\n")

    assert edit_and_run(repo, new) == 1

    assert "TestThing.test_method" in capsys.readouterr().out


def test_deleting_conftest_fails(repo: GitSandbox) -> None:
    repo.write("tests/conftest.py", "import pytest\n")
    repo.commit("add conftest")
    base = repo.head()
    repo.branch("feature")
    repo.delete("tests/conftest.py")
    repo.commit()

    assert run_guard(repo, base) == 1


def test_a_renamed_test_function_with_the_same_body_is_not_a_deletion(repo: GitSandbox) -> None:
    assert edit_and_run(repo, BASE_TESTS.replace("def test_adds", "def test_addition_works")) == 0


def test_moving_a_test_to_another_file_unchanged_is_not_a_deletion(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write(
        TEST_PATH, BASE_TESTS.replace('def test_raises():\n    with pytest.raises(ValueError):\n        int("x")\n', "")
    )
    repo.write(
        "tests/test_moved.py",
        'import pytest\n\n\ndef test_raises():\n    with pytest.raises(ValueError):\n        int("x")\n',
    )
    repo.commit()

    assert run_guard(repo, base) == 0


def test_renaming_a_test_file_without_changing_it_passes(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.move(TEST_PATH, "tests/test_renamed.py")
    repo.commit()

    assert run_guard(repo, base) == 0


def test_renaming_a_test_file_and_dropping_a_test_fails(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.move(TEST_PATH, "tests/test_renamed.py")
    repo.write("tests/test_renamed.py", BASE_TESTS.replace("def test_adds", "def helper_adds"))
    repo.commit()

    assert run_guard(repo, base) == 1


def test_renaming_a_test_file_so_pytest_no_longer_collects_it_fails(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.move(TEST_PATH, "tests/sample_checks.py")  # not test_*.py or *_test.py
    repo.commit()

    assert run_guard(repo, base) == 1

    assert "collect" in capsys.readouterr().out


# --- skipped / xfail-marked tests -------------------------------------------------------------------


SKIP_VARIANTS = {
    "mark.skip": BASE_TESTS.replace("def test_adds", '@pytest.mark.skip(reason="later")\ndef test_adds'),
    "mark.skipif": BASE_TESTS.replace("def test_adds", '@pytest.mark.skipif(True, reason="later")\ndef test_adds'),
    "mark.xfail": BASE_TESTS.replace("def test_adds", "@pytest.mark.xfail\ndef test_adds"),
    "pytest.skip call": BASE_TESTS.replace("def test_adds():\n", 'def test_adds():\n    pytest.skip("later")\n'),
    "pytest.xfail call": BASE_TESTS.replace("def test_adds():\n", 'def test_adds():\n    pytest.xfail("later")\n'),
    "importorskip": BASE_TESTS.replace("def test_adds():\n", 'def test_adds():\n    pytest.importorskip("numpy")\n'),
    "class-level skip": BASE_TESTS.replace("class TestThing:", '@pytest.mark.skip(reason="x")\nclass TestThing:'),
    "module-level pytestmark": BASE_TESTS.replace(
        "def test_adds", "pytestmark = pytest.mark.skip\n\n\ndef test_adds", 1
    ),
    "skip inside parametrize": BASE_TESTS.replace(
        "def test_adds",
        '@pytest.mark.parametrize("n", [1, pytest.param(2, marks=pytest.mark.skip)])\ndef test_adds',
    ),
    "unittest.skip": "import unittest\n"
    + BASE_TESTS.replace("def test_adds", '@unittest.skip("later")\ndef test_adds'),
    "from-import skip": "from unittest import skip\n"
    + BASE_TESTS.replace("def test_adds", '@skip("later")\ndef test_adds'),
}


ALIASED_SKIP_VARIANTS = {
    "import pytest as pt": "import pytest as pt\n\n\n@pt.mark.skip\ndef test_adds():\n    assert 1 + 1 == 2\n",
    "from pytest import mark as m": "from pytest import mark as m\n\n\n@m.xfail\ndef test_adds():\n    assert 1 + 1 == 2\n",
    "from pytest import skip": "from pytest import skip\n\n\ndef test_adds():\n    skip('later')\n    assert 1 + 1 == 2\n",
}


@pytest.mark.parametrize("variant", sorted(ALIASED_SKIP_VARIANTS))
def test_skip_markers_reached_through_an_import_alias_are_still_caught(variant: str, repo: GitSandbox) -> None:
    repo.write(TEST_PATH, "def test_adds():\n    assert 1 + 1 == 2\n")
    repo.commit("simple base")

    assert edit_and_run(repo, ALIASED_SKIP_VARIANTS[variant]) == 1


def test_a_variable_that_merely_has_skip_in_its_name_is_not_a_marker(repo: GitSandbox) -> None:
    new = (
        BASE_TESTS
        + "\n\ndef test_options():\n    options = type('O', (), {'skip': False})()\n    assert options.skip is False\n"
    )

    assert edit_and_run(repo, new) == 0


def test_moving_an_already_skipped_test_to_another_file_is_not_a_new_skip(repo: GitSandbox) -> None:
    skipped = "import pytest\n\n\n@pytest.mark.skip(reason='old')\ndef test_old():\n    assert 5 == 5\n"
    repo.write("tests/test_one.py", skipped)
    repo.commit("skipped test lives in one.py")
    base = repo.head()
    repo.branch("feature")
    repo.move("tests/test_one.py", "tests/test_two.py")
    repo.commit()

    assert run_guard(repo, base) == 0


@pytest.mark.parametrize("variant", sorted(SKIP_VARIANTS))
def test_adding_a_skip_or_xfail_marker_fails(
    variant: str, repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert edit_and_run(repo, SKIP_VARIANTS[variant]) == 1

    assert "skip" in capsys.readouterr().out.lower()


def test_a_new_test_added_already_skipped_fails(repo: GitSandbox) -> None:
    new = BASE_TESTS + '\n\n@pytest.mark.skip(reason="x")\ndef test_new():\n    assert True is True\n'

    assert edit_and_run(repo, new) == 1


def test_a_skip_that_was_already_there_is_not_blamed_on_this_change(repo: GitSandbox) -> None:
    skipped = SKIP_VARIANTS["mark.skip"]
    repo.write(TEST_PATH, skipped)
    repo.commit("skip already present")

    assert edit_and_run(repo, skipped + "\n\ndef test_extra():\n    assert 3 == 3\n") == 0


def test_removing_a_skip_marker_passes(repo: GitSandbox) -> None:
    repo.write(TEST_PATH, SKIP_VARIANTS["mark.skip"])
    repo.commit("skip present")

    assert edit_and_run(repo, BASE_TESTS) == 0


def test_skip_words_inside_strings_and_comments_are_not_markers(repo: GitSandbox) -> None:
    new = (
        BASE_TESTS
        + '\n\ndef test_text():\n    # pytest.mark.skip is discussed here\n    assert "pytest.mark.skip" != ""\n'
    )

    assert edit_and_run(repo, new) == 0


def test_adding_a_skip_in_conftest_fails(repo: GitSandbox) -> None:
    repo.write("tests/conftest.py", "import pytest\n")
    repo.commit("conftest")
    new = (
        "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n"
        '    for item in items:\n        item.add_marker(pytest.mark.skip(reason="x"))\n'
    )

    assert edit_and_run(repo, new, "tests/conftest.py") == 1


def test_a_collect_ignore_added_to_conftest_fails(repo: GitSandbox) -> None:
    repo.write("tests/conftest.py", "import pytest\n")
    repo.commit("conftest")

    assert edit_and_run(repo, 'import pytest\n\ncollect_ignore = ["test_sample.py"]\n', "tests/conftest.py") == 1


# --- loosened tests ---------------------------------------------------------------------------------


LOOSENED = {
    "an assert removed": BASE_TESTS.replace("    assert 2 + 2 == 4\n", ""),
    "body replaced by pass": BASE_TESTS.replace("    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n", "    pass\n"),
    "assert replaced by assert True": BASE_TESTS.replace("assert 1 + 1 == 2", "assert True"),
    "assert replaced by a constant": BASE_TESTS.replace("assert 1 + 1 == 2", "assert 1"),
    "pytest.raises removed": BASE_TESTS.replace(
        '    with pytest.raises(ValueError):\n        int("x")\n', '    int("0")\n'
    ),
    "assert commented out": BASE_TESTS.replace("    assert 2 + 2 == 4\n", "    # assert 2 + 2 == 4\n"),
}


@pytest.mark.parametrize("variant", sorted(LOOSENED))
def test_loosening_a_test_fails(variant: str, repo: GitSandbox) -> None:
    assert edit_and_run(repo, LOOSENED[variant]) == 1


def test_a_widened_numeric_tolerance_fails(repo: GitSandbox, capsys: pytest.CaptureFixture[str]) -> None:
    tight = BASE_TESTS + "\n\ndef test_close():\n    assert 0.1 + 0.2 == pytest.approx(0.3, rel=1e-9)\n"
    repo.write(TEST_PATH, tight)
    repo.commit("tight tolerance")

    assert edit_and_run(repo, tight.replace("rel=1e-9", "rel=0.5")) == 1

    assert "tolerance" in capsys.readouterr().out


def test_a_tighter_numeric_tolerance_passes(repo: GitSandbox) -> None:
    loose = BASE_TESTS + "\n\ndef test_close():\n    assert 0.1 + 0.2 == pytest.approx(0.3, rel=1e-3)\n"
    repo.write(TEST_PATH, loose)
    repo.commit("loose tolerance")

    assert edit_and_run(repo, loose.replace("rel=1e-3", "rel=1e-9")) == 0


def test_unittest_style_assertion_removal_is_caught(repo: GitSandbox) -> None:
    unit = (
        "import unittest\n\n\nclass CaseA(unittest.TestCase):\n    def test_x(self):\n"
        "        self.assertEqual(1, 1)\n        self.assertTrue(True)\n"
    )
    repo.write("tests/test_unit.py", unit)
    repo.commit("unittest style")

    assert edit_and_run(repo, unit.replace("        self.assertTrue(True)\n", ""), "tests/test_unit.py") == 1


def test_removing_an_assert_from_a_shared_helper_in_a_test_file_fails(repo: GitSandbox) -> None:
    helper = "def check_ok(value):\n    assert value is not None\n    assert value != ''\n\n\ndef test_uses(): \n    check_ok('a')\n    assert True is not False\n"
    repo.write("tests/test_helpers.py", helper)
    repo.commit("helper")

    assert edit_and_run(repo, helper.replace("    assert value != ''\n", ""), "tests/test_helpers.py") == 1


# --- changes that are fine ------------------------------------------------------------------------


def test_adding_new_tests_and_asserts_passes(repo: GitSandbox) -> None:
    new = BASE_TESTS.replace("    assert 2 + 2 == 4\n", "    assert 2 + 2 == 4\n    assert 3 + 3 == 6\n")
    new += "\n\ndef test_brand_new():\n    assert sorted([2, 1]) == [1, 2]\n"

    assert edit_and_run(repo, new) == 0


def test_a_brand_new_test_file_with_real_assertions_is_not_flagged(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    # The guard must not flag its own new tests.
    base = repo.head()
    repo.branch("feature")
    repo.write("tests/test_guard_new.py", "def test_new():\n    assert [1, 2] == [1, 2]\n")
    repo.commit()

    assert run_guard(repo, base) == 0

    assert "OK" in capsys.readouterr().out


def test_formatting_and_comment_only_edits_pass(repo: GitSandbox) -> None:
    new = BASE_TESTS.replace("assert 1 + 1 == 2", "assert (  # a comment\n        1 + 1 == 2\n    )")

    assert edit_and_run(repo, new) == 0


def test_changes_outside_test_files_are_ignored(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.write("src/app.py", "def add(a, b):\n    # pytest.mark.skip\n    return a + b\n")
    repo.commit()

    assert run_guard(repo, base) == 0


def test_a_test_file_that_no_longer_parses_fails_instead_of_passing_silently(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert edit_and_run(repo, "def test_broken(:\n    assert True\n") == 1

    assert "parse" in capsys.readouterr().out


def test_nested_helper_functions_inside_a_test_are_not_tests_of_their_own(repo: GitSandbox) -> None:
    with_helper = BASE_TESTS.replace(
        "def test_adds():\n",
        "def test_adds():\n    def inner():\n        return 2\n\n    assert inner() == 2\n",
    )
    repo.write(TEST_PATH, with_helper)
    repo.commit("nested helper")

    assert (
        edit_and_run(
            repo,
            with_helper.replace(
                "    def inner():\n        return 2\n\n    assert inner() == 2\n", "    assert 2 == 2\n"
            ),
        )
        == 0
    )


# --- pytest configuration --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_config",
    [
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra"\n',  # --strict-markers dropped
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra --strict-markers --deselect tests/test_sample.py"\n',
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra --strict-markers --ignore=tests/test_sample.py"\n',
        '[tool.pytest.ini_options]\ntestpaths = []\naddopts = "-ra --strict-markers"\n',
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra --strict-markers"\nnorecursedirs = ["tests"]\n',
    ],
)
def test_loosening_the_pytest_configuration_fails(repo: GitSandbox, new_config: str) -> None:
    assert edit_and_run(repo, new_config, "pyproject.toml") == 1


def test_adding_a_marker_declaration_to_the_pytest_configuration_passes(repo: GitSandbox) -> None:
    new_config = (
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra --strict-markers"\n'
        'markers = ["slow: takes longer"]\n'
    )

    assert edit_and_run(repo, new_config, "pyproject.toml") == 0


def test_unrelated_pyproject_changes_pass(repo: GitSandbox) -> None:
    new_config = (
        '[project]\nname = "x"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-ra --strict-markers"\n'
    )

    assert edit_and_run(repo, new_config, "pyproject.toml") == 0


# --- approval, bases, standalone use ---------------------------------------------------------------


def test_the_owner_approval_flag_lets_findings_through_but_still_prints_them(
    repo: GitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert edit_and_run(repo, SKIP_VARIANTS["mark.skip"], TEST_PATH, "--approved") == 0

    out = capsys.readouterr().out
    assert "skip" in out.lower()
    assert "approved" in out.lower()


def test_first_push_of_a_new_branch_compares_against_main(repo: GitSandbox) -> None:
    repo.branch("feature")
    repo.delete(TEST_PATH)
    repo.commit()

    assert run_guard(repo, ZEROS) == 1


def test_a_bad_head_ref_is_a_plain_error(repo: GitSandbox) -> None:
    assert guard_test_integrity.main(["--repo", str(repo.path), "--base", "main", "--head", "nope"]) == 2


def test_the_script_runs_on_its_own_the_way_ci_runs_it(repo: GitSandbox) -> None:
    base = repo.head()
    repo.branch("feature")
    repo.delete(TEST_PATH)
    repo.commit()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", "HEAD"], cwd=repo.path, capture_output=True, text=True
    )

    assert result.returncode == 1
    assert TEST_PATH in result.stdout


# --- first push of a ticket branch cut from an integration branch that diverged from main -------------


def test_first_push_of_a_ticket_branch_is_judged_against_the_nearest_fallback_not_old_main(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path / "diverged")  # fresh-start deleted tests/test_old.py long before this branch
    build_diverged_history(sandbox)
    repo = str(sandbox.path)

    both = ["--fallback-ref", "main", "--fallback-ref", "fresh-start"]
    assert guard_test_integrity.main(["--repo", repo, "--base", ZEROS, *both]) == 0
    assert guard_test_integrity.main(["--repo", repo, "--base", ZEROS, "--fallback-ref", "main"]) == 1


def test_first_push_of_a_ticket_branch_with_no_usable_fallback_is_judged_on_the_whole_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox = GitSandbox(tmp_path / "diverged")
    build_diverged_history(sandbox)
    sandbox.write("tests/test_skipped.py", "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert 1 == 1\n")
    sandbox.commit("a skipped test")

    code = guard_test_integrity.main(["--repo", str(sandbox.path), "--base", ZEROS, "--fallback-ref", "nope"])

    assert code == 1  # every file counts as new, and a new skipped test is a finding
    assert "tests/test_skipped.py" in capsys.readouterr().out

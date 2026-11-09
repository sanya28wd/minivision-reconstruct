"""Runner for JavaScript unit tests and syntax checks (CHORE-JS-TESTS).

Discovers and executes tests/*.test.cjs with node and runs node --check on all
static frontend scripts in src/incident_report/web/static/js/.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"
_STATIC_JS_DIR = _REPO_ROOT / "src" / "incident_report" / "web" / "static" / "js"

CJS_TEST_FILES = sorted(_TESTS_DIR.glob("*.test.cjs"))
STATIC_JS_FILES = sorted(_STATIC_JS_DIR.glob("*.js"))


def _ensure_node() -> str:
    """Ensure node is available on PATH, failing immediately if absent."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.fail("node is required for the JS unit tests")
    return node_bin


def test_cjs_test_files_discovered() -> None:
    """Verify at least one JS unit test file is present."""
    assert len(CJS_TEST_FILES) >= 1, "Expected at least one tests/*.test.cjs file"


@pytest.mark.parametrize("cjs_path", CJS_TEST_FILES, ids=lambda p: p.name)
def test_js_unit(cjs_path: Path) -> None:
    """Run a JS unit test file using node."""
    node_bin = _ensure_node()
    proc = subprocess.run(
        [node_bin, str(cjs_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_REPO_ROOT,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"JS unit test failed: {cjs_path.name} (exit code {proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )


def test_static_js_files_discovered() -> None:
    """Verify static JS files are present."""
    assert len(STATIC_JS_FILES) >= 1, "Expected at least one static JS file in src/.../static/js/"


@pytest.mark.parametrize("js_path", STATIC_JS_FILES, ids=lambda p: p.name)
def test_static_js_syntax_check(js_path: Path) -> None:
    """Run node --check on a static frontend JavaScript file."""
    node_bin = _ensure_node()
    proc = subprocess.run(
        [node_bin, "--check", str(js_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_REPO_ROOT,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"JS syntax check failed: {js_path.name} (exit code {proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )


def test_node_missing_fails_without_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that when node is not on PATH, the test fails explicitly rather than skipping."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.fail.Exception, match="node is required for the JS unit tests"):
        _ensure_node()

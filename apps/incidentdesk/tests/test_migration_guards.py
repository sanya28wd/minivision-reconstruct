"""Migration protections and the offline frozen-lock boundary."""

from pathlib import Path

import pytest

import check_dependencies
from guard_protected_paths import is_protected


@pytest.mark.parametrize(
    "path",
    [
        "apps/incidentdesk/docs/adr/0006.md",
        "apps/incidentdesk/GLOSSARY.md",
        "apps/incidentdesk/scripts/guardlib.py",
        "src/minivision_reconstruct/domain.py",
        "tests/smoke/test_example.py",
        "evaluation/results.json",
        "configs/config.yaml",
        "data/registry.csv",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "IMPLEMENTATION_PLAN.md",
    ],
)
def test_migration_paths_are_protected(path: str) -> None:
    assert is_protected(path)


def test_offline_import_rejects_a_changed_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("changed lock", encoding="utf-8")
    assert check_dependencies.main(["--repo", str(tmp_path), "--frozen-import", "--skip-imports"]) == 2

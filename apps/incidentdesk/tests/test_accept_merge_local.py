"""Acceptance helpers leave publication to the owner."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("suffix", ["sh", "ps1"])
def test_accept_merge_never_pushes(suffix: str) -> None:
    source = (Path(__file__).parents[1] / "scripts" / f"accept_merge.{suffix}").read_text(encoding="utf-8")
    assert "git push" not in source
    assert "Invoke-Git push" not in source
    assert "never pushes" in source

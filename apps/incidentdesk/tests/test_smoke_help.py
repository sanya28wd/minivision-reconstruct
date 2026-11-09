"""The owner smoke command's --help names the key variable it actually reads (chore: retro diagnostics)."""

from __future__ import annotations

import pytest

from incident_report.ai import smoke


def test_help_tells_the_owner_how_to_copy_one_of_their_numbered_keys(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        smoke.main(["--help"])

    assert exit_info.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "Set GEMINI_API_KEY from one of your keys, e.g. `$env:GEMINI_API_KEY = $env:GOOGLE_API_KEY_1`" in text
    assert "without printing it" in text

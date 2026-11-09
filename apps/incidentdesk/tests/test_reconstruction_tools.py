"""Fixed local tool links and a visible fallback without a proxy."""

import pytest


def test_fixed_tool_links_cannot_include_paths() -> None:
    from incident_report.reconstruction.tools import tool_links

    assert tool_links() == (
        ("Track review", "/reconstruct/tracks/"),
        ("Calibration", "/reconstruct/calibration/"),
        ("Reconstruction replay", "/reconstruct/replay/"),
    )


def test_proxy_absent_has_visible_fallback() -> None:
    from incident_report.reconstruction.tools import unavailable

    assert "reconstruction tools are not running" in unavailable("tracks").lower()
    with pytest.raises(ValueError):
        unavailable("../../etc")

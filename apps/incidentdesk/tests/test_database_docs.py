"""Database examples distinguish the admin connection from app storage."""

from pathlib import Path


def test_database_example_and_gate_comments() -> None:
    root = Path(__file__).parents[1]
    example = (root / ".env.test.example").read_text(encoding="utf-8")
    assert "legacy example" not in example
    assert "never use the donor database" not in example.lower()
    assert "throwaway test databases" in example
    for suffix in ["sh", "ps1"]:
        source = (root / "scripts" / f"check_all.{suffix}").read_text(encoding="utf-8")
        assert "standard port" not in source
        assert "5433/postgres" in source

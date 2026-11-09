"""Test that the rule engine module is isolated and pure (RPT-15, ADR 0007).

Constraints enforced:
- No model calls inside the rule engine.
- Fails if the rule-engine module imports the AI package or any network/DB module.
- Fails if non-deterministic modules like random or time are imported for logic.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_rule_engine_does_not_import_ai_network_or_database() -> None:
    rules_dir = Path(__file__).parent.parent / "src" / "incident_report" / "rules"
    forbidden_modules = {
        "incident_report.ai",
        "google",
        "google.genai",
        "psycopg",
        "requests",
        "urllib",
        "httpx",
        "aiohttp",
        "socket",
        "time",
        "random",
        "sqlite3",
        "asyncio",
    }

    rule_files = list(rules_dir.glob("*.py"))
    assert len(rule_files) > 0, "No python files found in rules directory"

    for py_file in rule_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    for forbidden in forbidden_modules:
                        assert not (name == forbidden or name.startswith(f"{forbidden}.")), (
                            f"{py_file.name} imports forbidden module '{name}'"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                for forbidden in forbidden_modules:
                    assert not (mod == forbidden or mod.startswith(f"{forbidden}.")), (
                        f"{py_file.name} imports from forbidden module '{mod}'"
                    )

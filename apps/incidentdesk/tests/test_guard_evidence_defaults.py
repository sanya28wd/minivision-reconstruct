"""The evidence-defaults guard: a missing value must mean unknown, never a confident default (ADR 0006)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import guard_evidence_defaults

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_evidence_defaults.py"


def make_repo(tmp_path: Path, facts: str, rules: dict[str, str] | None = None) -> Path:
    package = tmp_path / "src" / "incident_report"
    package.mkdir(parents=True)
    (package / "facts.py").write_text(facts, encoding="utf-8")
    for name, text in (rules or {}).items():
        (package / "rules").mkdir(exist_ok=True)
        (package / "rules" / name).write_text(text, encoding="utf-8")
    return tmp_path


def run_guard(repo: Path) -> int:
    return guard_evidence_defaults.main(["--repo", str(repo)])


HEADER = "from dataclasses import dataclass, field\nfrom pydantic import BaseModel, Field\n\n"


def test_the_confidence_one_point_zero_bug_is_caught(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path, HEADER + "class Fact(BaseModel):\n    confidence: float = 1.0\n")

    assert run_guard(repo) == 1

    out = capsys.readouterr().out
    assert "Fact.confidence" in out
    assert "facts.py:5" in out


@pytest.mark.parametrize(
    "line",
    [
        "x: float = Field(default=0.5, ge=0)",
        "x: float = Field(0.5)",
        "x: list[str] = Field(default_factory=list)",
        "x: int = 0",
        "x: str | None = 'a'",
        "x: int = None",  # None default but not typed as optional
    ],
)
def test_other_defaults_on_a_pydantic_model_are_caught(tmp_path: Path, line: str) -> None:
    repo = make_repo(tmp_path, HEADER + f"class M(BaseModel):\n    {line}\n")

    assert run_guard(repo) == 1


@pytest.mark.parametrize(
    "line",
    [
        "x: int = 0",
        "x: list[str] = field(default_factory=list)",
        "x: str = field(default='a')",
        "x: int = None",
    ],
)
def test_defaults_on_a_dataclass_are_caught(tmp_path: Path, line: str) -> None:
    repo = make_repo(tmp_path, HEADER + f"@dataclass(frozen=True)\nclass D:\n    {line}\n")

    assert run_guard(repo) == 1


def test_a_class_derived_from_a_model_in_the_same_file_is_checked(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, HEADER + "class A(BaseModel):\n    a: int\n\nclass B(A):\n    b: int = 3\n")

    assert run_guard(repo) == 1


@pytest.mark.parametrize(
    "line",
    [
        "x: float",
        "x: float = Field(ge=0.0, le=1.0)",
        "x: float = Field(..., ge=0)",
        "x: str | None = None",
        "x: None | str = None",
        "x: Optional[str] = None",
        "x: str | None = Field(default=None, min_length=1)",
        "x: str | None = Field(None)",
        "x: ClassVar[int] = 3",
        "x: int = 3  # default-ok: a count of nothing yet is genuinely zero",
    ],
)
def test_required_fields_none_for_unknown_and_marked_defaults_pass(tmp_path: Path, line: str) -> None:
    source = "from typing import ClassVar, Optional\n" + HEADER + f"class M(BaseModel):\n    {line}\n"

    assert run_guard(make_repo(tmp_path, source)) == 0


def test_a_default_ok_comment_needs_a_reason(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, HEADER + "class M(BaseModel):\n    x: int = 3  # default-ok:\n")

    assert run_guard(repo) == 1


def test_the_comment_may_sit_on_any_line_of_a_multiline_field(tmp_path: Path) -> None:
    source = (
        HEADER
        + "class M(BaseModel):\n    x: int = Field(\n        default=3,  # default-ok: documented zero point\n    )\n"
    )

    assert run_guard(make_repo(tmp_path, source)) == 0


def test_model_config_and_plain_classes_and_function_locals_are_ignored(tmp_path: Path) -> None:
    source = HEADER + (
        "class M(BaseModel):\n    model_config = {'frozen': True}\n    x: int\n\n"
        "class Plain:\n    y: int = 5\n\n"
        "def f() -> None:\n    z: int = 5\n    print(z)\n"
    )

    assert run_guard(make_repo(tmp_path, source)) == 0


def test_the_rules_package_is_scanned_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(
        tmp_path,
        HEADER + "class Ok(BaseModel):\n    x: int\n",
        {"fault.py": HEADER + "@dataclass\nclass Verdict:\n    score: float = 1.0\n"},
    )

    assert run_guard(repo) == 1

    assert "rules/fault.py" in capsys.readouterr().out


def test_a_file_that_does_not_parse_is_an_error_not_a_pass(tmp_path: Path) -> None:
    assert run_guard(make_repo(tmp_path, "class (:\n")) == 2


def test_no_files_to_check_is_an_error_not_a_pass(tmp_path: Path) -> None:
    assert run_guard(tmp_path) == 2


def test_the_command_line_accepts_the_common_base_flag(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, HEADER + "class M(BaseModel):\n    x: int\n")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--base", "fresh-start"], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout

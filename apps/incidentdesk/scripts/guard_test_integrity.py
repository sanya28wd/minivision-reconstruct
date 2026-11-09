#!/usr/bin/env python3
"""Test-integrity guard (RPT-02).

Spec 001 "Testing Decisions": tests may not be deleted, skipped or weakened without owner approval.
Research note AI_ASSISTED_PRODUCTION_ENGINEERING.md section 2: agents game tests by deleting them,
skipping them, or loosening them. This guard compares the test files at the base and head of a change
and fails when it finds:

  * a deleted test file, or a deleted test function/method (a rename or move with the same body is fine);
  * a test file renamed so pytest no longer collects it;
  * more skip / skipif / xfail / importorskip / unittest.skip markers or calls, or a collect_ignore;
  * fewer assertions in a function, an assertion made trivially true (`assert True`), or a widened
    numeric tolerance (`approx(..., rel=...)`, `rtol`, `atol`, `delta`, fewer `places`);
  * a looser pytest configuration (a dropped --strict-markers, added --deselect/--ignore/-k, narrower
    testpaths, changed norecursedirs/python_files).

It works on the Python syntax tree, so comments, strings and formatting changes never trigger it.
It is a tripwire, not a proof: a reviewer still reads the test diff. Pass --approved (the owner-approval
label) to let findings through; they are still printed.

Usage (same in CI and locally):
    python scripts/guard_test_integrity.py --base origin/main --head HEAD [--approved]
"""

from __future__ import annotations

import argparse
import ast
import configparser
import copy
import shlex
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from guardlib import (
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_OK,
    Change,
    GuardError,
    Range,
    add_common_arguments,
    changed_files,
    file_at,
    resolve_range,
)

# Calls and markers that stop a test from running or from counting.
SKIP_NAMES = frozenset(
    {
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
        "pytest.skip",
        "pytest.xfail",
        "pytest.importorskip",
        "unittest.skip",
        "unittest.skipIf",
        "unittest.skipUnless",
        "unittest.expectedFailure",
    }
)
SKIP_METHOD_SUFFIXES = (".skipTest",)  # self.skipTest(...) in unittest.TestCase
COLLECT_IGNORE_NAMES = frozenset({"collect_ignore", "collect_ignore_glob"})
# Calls that count as an assertion besides the assert statement. Methods/functions named assert* also count.
ASSERTING_CALLS = frozenset({"pytest.raises", "pytest.warns", "pytest.deprecated_call"})
# Keyword arguments where a bigger number means a looser comparison (and `places`, where smaller is looser).
TOLERANCE_LARGER_IS_LOOSER = frozenset(
    {"rel", "abs", "rtol", "atol", "rel_tol", "abs_tol", "delta", "tol", "tolerance"}
)
TOLERANCE_SMALLER_IS_LOOSER = frozenset({"places"})

PYTEST_CONFIG_FILES = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")
# pytest options whose change can silently drop tests from a run.
COLLECTION_OPTIONS = ("norecursedirs", "python_files", "python_functions", "python_classes")
LOOSENING_ADDOPTS = ("--deselect", "--ignore", "--ignore-glob", "-k")
STRICT_ADDOPTS = ("--strict-markers", "--strict-config", "--strict")


# --- which files count as tests ----------------------------------------------------------------------


def is_collectable(path: str) -> bool:
    """Files pytest collects by default (python_files = test_*.py *_test.py)."""
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_conftest(path: str) -> bool:
    return PurePosixPath(path).name == "conftest.py"


def in_test_scope(path: str) -> bool:
    """Python files whose assertions and skips we watch: tests, conftest, helpers inside tests/."""
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return False
    return is_collectable(path) or is_conftest(path) or any(part in ("tests", "test") for part in p.parts[:-1])


# --- reading a test file -----------------------------------------------------------------------------


@dataclass
class Counts:
    skips: int = 0
    asserts: int = 0
    trivial_asserts: int = 0
    tolerances: dict[str, float] = field(default_factory=dict)

    def add(self, other: Counts) -> None:
        self.skips += other.skips
        self.asserts += other.asserts
        self.trivial_asserts += other.trivial_asserts


@dataclass
class Unit:
    """A function or method, the smallest piece we compare. Nested functions belong to their parent."""

    qualname: str
    is_test: bool
    dump: str  # the syntax tree without the function's own name, to recognise renames and moves
    counts: Counts


@dataclass
class ModuleInfo:
    units: dict[str, Unit] = field(default_factory=dict)
    module_counts: Counts = field(default_factory=Counts)  # everything outside the units
    parse_error: str | None = None


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """local name -> real dotted name, for `import pytest as pt` and `from pytest import mark as m`."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _resolve(node: ast.AST, aliases: dict[str, str]) -> str | None:
    dotted = _dotted(node)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    real = aliases.get(head, head)
    return f"{real}.{rest}" if rest else real


def _is_trivially_true(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_is_trivially_true(v) for v in test.values)
    return False


def _number(node: ast.expr) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _number(node.operand)
        return None if inner is None else -inner
    return None


def _count(root: ast.AST, aliases: dict[str, str]) -> Counts:
    counts = Counts()
    for node in ast.walk(root):
        if isinstance(node, ast.Assert):
            counts.asserts += 1
            if _is_trivially_true(node.test):
                counts.trivial_asserts += 1
        elif isinstance(node, ast.Call):
            resolved = _resolve(node.func, aliases)
            if resolved in ASSERTING_CALLS:
                counts.asserts += 1
            elif isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert"):
                counts.asserts += 1
            elif isinstance(node.func, ast.Name) and node.func.id.startswith("assert"):
                counts.asserts += 1
            for kw in node.keywords:
                value = _number(kw.value)
                if kw.arg and value is not None:
                    if kw.arg in TOLERANCE_LARGER_IS_LOOSER:
                        counts.tolerances[kw.arg] = max(counts.tolerances.get(kw.arg, value), value)
                    elif kw.arg in TOLERANCE_SMALLER_IS_LOOSER:
                        counts.tolerances[kw.arg] = min(counts.tolerances.get(kw.arg, value), value)
        if isinstance(node, ast.Attribute | ast.Name):
            resolved = _resolve(node, aliases)
            if resolved in SKIP_NAMES or (
                isinstance(node, ast.Attribute) and resolved and resolved.endswith(SKIP_METHOD_SUFFIXES)
            ):
                counts.skips += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in COLLECT_IGNORE_NAMES:
                    size = len(node.value.elts) if isinstance(node.value, ast.List | ast.Tuple) else 1
                    counts.skips += max(1, size)
    return counts


def _unit_dump(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = copy.deepcopy(node)
    clone.name = "_"
    return ast.dump(clone)


def analyse(source: str) -> ModuleInfo:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return ModuleInfo(parse_error=str(exc))
    aliases = _import_aliases(tree)
    info = ModuleInfo()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = f"{prefix}{node.name}"
                info.units[qualname] = Unit(
                    qualname, node.name.startswith("test"), _unit_dump(node), _count(node, aliases)
                )
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    total = _count(tree, aliases)
    in_units = Counts()
    for unit in info.units.values():
        in_units.add(unit.counts)
    info.module_counts = Counts(
        skips=max(0, total.skips - in_units.skips),
        asserts=max(0, total.asserts - in_units.asserts),
        trivial_asserts=max(0, total.trivial_asserts - in_units.trivial_asserts),
    )
    return info


# --- comparing base and head -------------------------------------------------------------------------


def _compare_counts(path: str, name: str, old: Counts, new: Counts, findings: list[str]) -> None:
    if new.skips > old.skips:
        findings.append(f"{path}: {name}: skip/xfail marker added (before {old.skips}, now {new.skips})")
    if new.asserts < old.asserts:
        findings.append(f"{path}: {name}: assertions removed (before {old.asserts}, now {new.asserts})")
    if new.trivial_asserts > old.trivial_asserts:
        findings.append(
            f"{path}: {name}: assertion made trivially true, e.g. `assert True` (now {new.trivial_asserts})"
        )
    for key, new_value in new.tolerances.items():
        old_value = old.tolerances.get(key)
        if old_value is None:
            continue
        looser = new_value < old_value if key in TOLERANCE_SMALLER_IS_LOOSER else new_value > old_value
        if looser:
            findings.append(f"{path}: {name}: tolerance loosened ({key}: {old_value:g} -> {new_value:g})")


def _read(repo: Path, ref: str, path: str | None) -> ModuleInfo | None:
    if path is None or not in_test_scope(path):
        return None
    text = file_at(repo, ref, path)
    return None if text is None else analyse(text)


@dataclass
class Entry:
    """One changed Python file in test scope, with its old and new contents analysed."""

    change: Change
    old: ModuleInfo
    new: ModuleInfo
    shown: str  # the path to name in messages (the old path for a delete or rename)


def _entries(repo: Path, rng: Range, changes: list[Change], findings: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    for change in changes:
        old_path = change.old_path if change.status == "R" else change.path
        old = None if change.status == "A" else _read(repo, rng.old, old_path)
        new = None if change.status == "D" else _read(repo, rng.head, change.path)
        if old is None and new is None:
            continue
        if new is not None and new.parse_error:
            findings.append(f"{change.path}: cannot parse this test file ({new.parse_error}), so it cannot be checked")
            continue
        if old is not None and old.parse_error:
            old = ModuleInfo()  # an unreadable old version has nothing to compare against
        entries.append(Entry(change, old or ModuleInfo(), new or ModuleInfo(), old_path or change.path))
    return entries


def check_python_files(repo: Path, rng: Range, changes: list[Change]) -> list[str]:
    findings: list[str] = []
    entries = _entries(repo, rng, changes, findings)

    # Functions that vanished from their file, and functions that appeared in one. A vanished test whose
    # body shows up unchanged as a new test somewhere is a rename or a move, not a deletion.
    removed = [
        (i, q, u)
        for i, e in enumerate(entries)
        for q, u in e.old.units.items()
        if (e.change.status == "D" or q not in e.new.units) and (u.is_test or u.counts.asserts)
    ]
    appeared = [
        (i, q, u)
        for i, e in enumerate(entries)
        for q, u in e.new.units.items()
        if e.change.status == "A" or q not in e.old.units
    ]
    taken: set[int] = set()
    came_from: dict[tuple[int, str], Unit] = {}  # (entry, qualname) of a moved unit -> its old self
    lost: list[tuple[int, str, Unit]] = []
    for i, q, unit in removed:
        match = next(
            (
                k
                for k, (_, _, cand) in enumerate(appeared)
                if k not in taken and cand.dump == unit.dump and cand.is_test == unit.is_test
            ),
            None,
        )
        if match is None:
            lost.append((i, q, unit))
        else:
            taken.add(match)
            came_from[(appeared[match][0], appeared[match][1])] = unit

    for i, entry in enumerate(entries):
        change = entry.change
        lost_here = [q for (j, q, _) in lost if j == i]
        if change.status == "D":
            if is_conftest(change.path) or lost_here or (is_collectable(change.path) and not entry.old.units):
                detail = f" (tests lost: {', '.join(lost_here)})" if lost_here else ""
                findings.append(f"{entry.shown}: test file deleted{detail}")
            continue
        for q in lost_here:
            unit = entry.old.units[q]
            kind = "test function" if unit.is_test else "helper with assertions"
            findings.append(f"{entry.shown}: {kind} removed: {q}")
        if change.status == "R" and is_collectable(change.old_path or "") and not is_collectable(change.path):
            findings.append(
                f"{entry.shown}: renamed to {change.path}, which pytest will not collect (tests silently dropped)"
            )
        for q, unit in entry.new.units.items():
            before = entry.old.units.get(q) or came_from.get((i, q))
            _compare_counts(change.path, q, before.counts if before else Counts(), unit.counts, findings)
        _compare_counts(change.path, "<module>", entry.old.module_counts, entry.new.module_counts, findings)
    return findings


# --- pytest configuration ----------------------------------------------------------------------------


def _pytest_options(path: str, text: str) -> dict[str, Any] | None:
    name = PurePosixPath(path).name
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            options = data.get("tool", {}).get("pytest", {}).get("ini_options")
            return dict(options) if isinstance(options, dict) else {}
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        for section in ("pytest", "tool:pytest"):
            if parser.has_section(section):
                return dict(parser.items(section))
        return {}
    except (tomllib.TOMLDecodeError, configparser.Error):
        return None


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    return []


def _option_tokens(options: dict[str, Any], key: str) -> list[str]:
    value = options.get(key)
    if key in ("testpaths", "norecursedirs", "python_files", "python_functions", "python_classes") and isinstance(
        value, str
    ):
        return value.split()
    return _tokens(value)


def check_pytest_config(repo: Path, rng: Range, changes: list[Change]) -> list[str]:
    findings: list[str] = []
    for change in changes:
        if PurePosixPath(change.path).name not in PYTEST_CONFIG_FILES or change.status in ("A", "D"):
            continue
        old_text, new_text = file_at(repo, rng.old, change.path), file_at(repo, rng.head, change.path)
        if old_text is None or new_text is None:
            continue
        old, new = _pytest_options(change.path, old_text), _pytest_options(change.path, new_text)
        if old is None or new is None:
            findings.append(f"{change.path}: cannot parse the pytest configuration, so it cannot be checked")
            continue

        old_add, new_add = _option_tokens(old, "addopts"), _option_tokens(new, "addopts")
        for token in new_add:
            if token.startswith(LOOSENING_ADDOPTS) and token not in old_add:
                findings.append(f"{change.path}: pytest addopts now contains {token}, which can drop tests from a run")
        for strict in STRICT_ADDOPTS:
            if strict in old_add and strict not in new_add:
                findings.append(f"{change.path}: pytest addopts no longer contains {strict}")
        for removed in sorted(set(_option_tokens(old, "testpaths")) - set(_option_tokens(new, "testpaths"))):
            findings.append(f"{change.path}: pytest testpaths no longer includes {removed}")
        for key in COLLECTION_OPTIONS:
            if _option_tokens(old, key) != _option_tokens(new, key) and key in new:
                findings.append(f"{change.path}: pytest option {key} changed, which can drop tests from collection")
        if old.get("xfail_strict") is True and new.get("xfail_strict") is not True:
            findings.append(f"{change.path}: pytest xfail_strict was turned off")
    return findings


# --- command line ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser)
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    try:
        rng = resolve_range(repo, args.base, args.head, args.fallback_ref)
        changes = changed_files(repo, rng, find_renames=True)
        findings = check_python_files(repo, rng, changes) + check_pytest_config(repo, rng, changes)
    except GuardError as exc:
        print(f"ERROR test-integrity guard could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"test-integrity guard: comparing {rng.old[:12]}..{rng.head[:12]} ({rng.note})")
    if not findings:
        print("OK no test was deleted, skipped, xfail-marked or loosened.")
        return EXIT_OK

    for finding in sorted(set(findings)):
        print(f"FINDING {finding}")
    if args.approved:
        print(f"OK {len(set(findings))} finding(s), but the owner-approval label is present (approved).")
        return EXIT_OK
    print(
        f"FAIL {len(set(findings))} test-integrity finding(s) without owner approval. Tests may not be deleted, "
        "skipped or weakened without the owner; if intended, the owner adds the approval label and CI re-runs."
    )
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

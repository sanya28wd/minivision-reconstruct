#!/usr/bin/env python3
"""Evidence-defaults guard (retro guard after RPT-11).

Fails when a field of a pydantic model or dataclass in src/incident_report/facts.py or
src/incident_report/rules/ has a default value, unless:
  - the field is typed `X | None` (or `Optional[X]`) and its default is `None` ("unknown"), or
  - the field's statement carries the comment `# default-ok: <reason>` (a reason is required).

Why (ADR 0006, traceability): a missing value must mean *unknown*, never a confident default. The RPT-11
bug was a `confidence = 1.0` default, which made a fact the model never scored look fully certain. A
required field forces the caller to say what it knows; `None` says "unknown" out loud. Empty-collection
defaults (`default_factory=list`) are checked like any other default, because "no facts" must be a
decision, not an accident.

The check reads the Python syntax tree (ast), so comments, strings and formatting never confuse it.
A class counts as a model when it is decorated with `@dataclass` or derives (directly, or through a class
in the same file) from `BaseModel`. `ClassVar` annotations and `model_config` are not fields.

Usage (same in CI and locally):
    python scripts/guard_evidence_defaults.py [--repo .] [--base ...]
`--base`, `--head` and `--fallback-ref` are accepted so every guard has the same command line, but this
guard always checks the whole current tree: a default that already exists is as dangerous as a new one.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from guardlib import EXIT_ERROR, EXIT_FAILED, EXIT_OK, add_common_arguments

TARGETS = ("src/incident_report/facts.py", "src/incident_report/rules")
ALLOW_COMMENT = re.compile(r"#\s*default-ok:\s*\S")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    owner: str
    field: str
    why: str


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    if isinstance(node, ast.Subscript):
        return _name_of(node.value)
    return ""


def _is_dataclass(cls: ast.ClassDef) -> bool:
    return any(_name_of(d) == "dataclass" for d in cls.decorator_list)


def _model_classes(tree: ast.Module) -> list[ast.ClassDef]:
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    model_names = {c.name for c in classes if any(_name_of(b) == "BaseModel" for b in c.bases)}
    changed = True
    while changed:  # a class derived from a model in this file is a model too
        changed = False
        for c in classes:
            if c.name not in model_names and any(_name_of(b) in model_names for b in c.bases):
                model_names.add(c.name)
                changed = True
    return [c for c in classes if c.name in model_names or _is_dataclass(c)]


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_optional(annotation: ast.expr) -> bool:
    """True for `X | None`, `None | X`, `Optional[X]` and `typing.Optional[X]`."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:  # a string annotation ("X | None") is parsed the same way
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return any(_is_none(side) or _is_optional(side) for side in (annotation.left, annotation.right))
    return isinstance(annotation, ast.Subscript) and _name_of(annotation.value) == "Optional"


def _default_of(value: ast.expr) -> ast.expr | str | None:
    """The default a field assignment gives, or None when it gives none.

    `Field(...)`/`field(...)` carry a default only through `default=`, `default_factory=` or a leading
    positional argument that is not `...`. Returns the default expression, or a label for a factory.
    """
    if isinstance(value, ast.Call) and _name_of(value.func) in {"Field", "field"}:
        for kw in value.keywords:
            if kw.arg == "default":
                return kw.value
            if kw.arg in {"default_factory", "factory"}:
                return f"{kw.arg}={ast.unparse(kw.value)}"
        if value.args and not (isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis):
            return value.args[0]
        return None
    return value


def _check_class(cls: ast.ClassDef, lines: list[str], path: str) -> list[Finding]:
    found: list[Finding] = []
    for stmt in cls.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None):
            continue
        name = stmt.target.id
        if name == "model_config" or _name_of(stmt.annotation) == "ClassVar":
            continue
        default = _default_of(stmt.value)
        if default is None:
            continue
        end = stmt.end_lineno or stmt.lineno
        if any(ALLOW_COMMENT.search(line) for line in lines[stmt.lineno - 1 : end]):
            continue
        if isinstance(default, ast.expr) and _is_none(default):
            if _is_optional(stmt.annotation):
                continue
            why = "defaults to None but is not typed `X | None`"
        else:
            shown = default if isinstance(default, str) else ast.unparse(default)
            why = f"has a default ({shown}); a missing value must mean unknown (ADR 0006)"
        found.append(Finding(path, stmt.lineno, cls.name, name, why))
    return found


def scan_file(path: Path, display: str) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=display)
    lines = source.splitlines()
    findings: list[Finding] = []
    for cls in _model_classes(tree):
        findings.extend(_check_class(cls, lines, display))
    return findings


def target_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for target in TARGETS:
        p = repo / target
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser, approval=False)
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    files = target_files(repo)
    if not files:
        print(
            f"ERROR evidence-defaults guard found nothing to check under {repo} ({', '.join(TARGETS)})", file=sys.stderr
        )
        return EXIT_ERROR
    findings: list[Finding] = []
    try:
        for f in files:
            findings.extend(scan_file(f, f.relative_to(repo).as_posix()))
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        print(f"ERROR evidence-defaults guard could not read a file: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"evidence-defaults guard: checked {len(files)} file(s)")
    if not findings:
        print("OK no model or dataclass field has a default value.")
        return EXIT_OK
    for item in findings:
        print(f"DEFAULT {item.path}:{item.line} {item.owner}.{item.field} {item.why}")
    print(
        f"FAIL {len(findings)} field(s) with a default. Make the field required, type it `X | None` with default "
        "None for unknown, or add `# default-ok: <reason>` on the line."
    )
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

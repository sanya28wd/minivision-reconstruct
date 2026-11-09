#!/usr/bin/env python3
"""Dependency checks for CI (RPT-02): the part of the audit that is not pip-audit.

1. Every package in uv.lock really exists on PyPI at that exact version, is not yanked, and the hashes
   pinned in the lock are hashes PyPI lists for that release. Models invent package names and attackers
   register them ("slopsquatting", Spracklen et al., USENIX Security 2025; research note
   AI_ASSISTED_PRODUCTION_ENGINEERING.md section 5). A lock entry from anywhere but PyPI also fails.
2. Packages this change ADDS to the lock (compared with the base) must be at least MIN_AGE_DAYS old as a
   project on PyPI. That is a floor, not proof of reputation: the facts (first release, release count,
   links) are printed so the reviewer can judge, and the PR should say why the dependency is needed.
   MIN_AGE_DAYS is a project assumption, to be validated: squatted look-alike names are typically
   registered shortly before they are used, so a brand-new project is the cheap signal to catch.
3. Every package the code imports is declared in pyproject.toml: application code (src/) in
   [project].dependencies, tests and scripts in dependencies or a dependency group. Importing something
   that is only installed because another package pulled it in is fragile (RPT-01 review, finding 3), and
   an import no installed package provides is a typo or a made-up name.

pip-audit (known vulnerabilities) is a separate command; see docs/CI_GUARDRAILS.md.

Usage:
    python scripts/check_dependencies.py --base origin/main --head HEAD
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any

from guardlib import (
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_OK,
    GuardError,
    add_common_arguments,
    file_at,
    git,
    resolve_range,
)

PYPI_API = "https://pypi.org/pypi"
PYPI_REGISTRY = "https://pypi.org/simple"  # what a PyPI entry in uv.lock records as its source
# Project assumption, to be validated: see the module docstring, point 2.
MIN_AGE_DAYS = 30
TIMEOUT_SECONDS = 20
ATTEMPTS = 3


# --- the lock file -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Locked:
    name: str
    version: str
    hashes: frozenset[str]  # sha256 hex digests
    registry: str | None  # None for a path/git/url source


def normalize(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(text: str) -> list[Locked]:
    """Packages in a uv.lock, without the project itself (an editable or virtual path source)."""
    data = tomllib.loads(text)
    packages: list[Locked] = []
    for entry in data.get("package", []):
        source = entry.get("source", {})
        if "editable" in source or "virtual" in source:
            continue
        hashes: set[str] = set()
        files = list(entry.get("wheels", []))
        if "sdist" in entry:
            files.append(entry["sdist"])
        for f in files:
            h = f.get("hash", "")
            if h.startswith("sha256:"):
                hashes.add(h.removeprefix("sha256:"))
        packages.append(Locked(normalize(entry["name"]), entry["version"], frozenset(hashes), source.get("registry")))
    return packages


# --- talking to the package index --------------------------------------------------------------------


class Index:
    """The PyPI JSON API (https://docs.pypi.org/api/json/)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, *parts: str) -> dict[str, Any] | None:
        """JSON for the given path, None if the index says 404, GuardError if it could not be asked."""
        url = "/".join([self.base_url, *parts, "json"])
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise GuardError(f"Refusing to query a non-HTTPS index: {url}")
        last: Exception | None = None
        for _ in range(ATTEMPTS):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "incident-report-ci-dependency-check"})  # noqa: S310
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                    return json.load(response)  # type: ignore[no-any-return]
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last = exc
        raise GuardError(f"Could not ask the package index about {url}: {last}")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def check_exist(index: Index, packages: list[Locked]) -> list[str]:
    problems: list[str] = []

    def check(pkg: Locked) -> list[str]:
        label = f"{pkg.name}=={pkg.version}"
        if pkg.registry != PYPI_REGISTRY:
            return [
                f"{label}: comes from {pkg.registry or 'a path, git or URL source'}, not PyPI, so it cannot be verified"
            ]
        data = index.get(pkg.name, pkg.version)
        if data is None:
            return [
                f"{label}: does not exist on PyPI (invented or mistyped package name, or a version never published)"
            ]
        files = data.get("urls", [])
        if not files or all(f.get("yanked") for f in files) or data.get("info", {}).get("yanked"):
            return [f"{label}: this release has been yanked by its maintainers or has no files"]
        known = {f.get("digests", {}).get("sha256") for f in files}
        missing = sorted(pkg.hashes - known)
        if not pkg.hashes:
            return [f"{label}: the lock pins no sha256 hash for it"]
        if missing:
            return [
                f"{label}: {len(missing)} hash(es) in uv.lock are not hashes PyPI lists for this release (e.g. {missing[0][:12]}...)"
            ]
        return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(check, packages):
            problems.extend(result)
    return problems


@dataclass
class NewPackageReport:
    problems: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)


def check_new_packages(index: Index, new: list[Locked], min_age_days: int, now: datetime) -> NewPackageReport:
    report = NewPackageReport()

    def check(pkg: Locked) -> tuple[list[str], list[str]]:
        label = f"{pkg.name}=={pkg.version}"
        data = index.get(pkg.name)
        if data is None:
            return [], []  # check_exist reports a project PyPI has never heard of
        if "releases" not in data:
            return [f"{label}: PyPI gave no release history, so its age cannot be checked"], []
        uploads = [
            _parse_time(f["upload_time_iso_8601"])
            for files in data["releases"].values()
            for f in files
            if f.get("upload_time_iso_8601")
        ]
        if not uploads:
            return [f"{label}: PyPI lists no uploaded files for this project"], []
        first, age = min(uploads), (now - min(uploads)).days
        urls = data.get("info", {}).get("project_urls") or {}
        link = next(iter(urls.values()), "no project link")
        fact = (
            f"NEW {label}: first release {first:%Y-%m-%d} ({age} days ago), {len(data['releases'])} release(s), {link}"
        )
        if age < min_age_days:
            return [f"{label}: new dependency whose project is only {age} days old (floor is {min_age_days})"], [fact]
        return [], [fact]

    with ThreadPoolExecutor(max_workers=8) as pool:
        for problems, facts in pool.map(check, new):
            report.problems.extend(problems)
            report.facts.extend(facts)
    return report


# --- imports versus declarations ---------------------------------------------------------------------


def _requirement_names(requirements: Iterable[Any]) -> set[str]:
    names = set()
    for requirement in requirements:
        if isinstance(requirement, str):
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                names.add(normalize(match.group(1)))
    return names


def declared_dependencies(pyproject: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(runtime, everything): names in [project].dependencies, and those plus groups and extras."""
    project = pyproject.get("project", {})
    runtime = _requirement_names(project.get("dependencies", []))
    everything = set(runtime)
    for extra in project.get("optional-dependencies", {}).values():
        everything |= _requirement_names(extra)
    for group in pyproject.get("dependency-groups", {}).values():
        everything |= _requirement_names(group)
    return runtime, everything


def imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _own_modules(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {
        p.stem if p.is_file() else p.name
        for p in directory.iterdir()
        if (p.is_file() and p.suffix == ".py") or p.is_dir()
    }


def check_imports(repo: Path, runtime_dirs: list[str], dev_dirs: list[str]) -> list[str]:
    try:
        pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GuardError(f"Cannot read pyproject.toml in {repo}: {exc}") from exc
    runtime_declared, all_declared = declared_dependencies(pyproject)
    providers = packages_distributions()  # import name -> distributions installed in this environment
    first_party = (
        set().union(*(_own_modules(repo / d) for d in [*runtime_dirs, *dev_dirs]))
        if runtime_dirs or dev_dirs
        else set()
    )
    ignorable = set(sys.stdlib_module_names) | first_party

    problems: list[str] = []
    for directory, allowed, scope in [(d, runtime_declared, "application") for d in runtime_dirs] + [
        (d, all_declared, "development") for d in dev_dirs
    ]:
        for path in sorted((repo / directory).rglob("*.py")):
            rel = path.relative_to(repo).as_posix()
            try:
                modules = imported_modules(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                problems.append(f"{rel}: cannot read imports ({exc})")
                continue
            for module in sorted(modules - ignorable):
                dists = {normalize(d) for d in providers.get(module, [])}
                if not dists:
                    problems.append(
                        f"{rel}: imports {module!r}, which no installed package provides (typo, invented name, or not installed)"
                    )
                elif not dists & allowed:
                    where = (
                        "[project].dependencies"
                        if scope == "application"
                        else "pyproject.toml (dependencies or a dependency group)"
                    )
                    hint = ""
                    if scope == "application" and dists & all_declared:
                        hint = " It is declared only for development, not for the application."
                    problems.append(
                        f"{rel}: imports {module!r} (package {sorted(dists)[0]}) but it is not declared in {where}; "
                        f"it is installed only because something else needs it.{hint}"
                    )
    return problems


# --- command line ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser, approval=False)
    parser.add_argument(
        "--index-url",
        default=PYPI_API,
        help=f"Explicit alternate index requests a full re-audit, even for an unchanged lock (default {PYPI_API})",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=MIN_AGE_DAYS,
        help=f"floor for the age of a NEW dependency's project (default {MIN_AGE_DAYS}; project assumption, to be validated)",
    )
    parser.add_argument("--runtime-dir", action="append", default=None, help="application code dir (default: src)")
    parser.add_argument("--dev-dir", action="append", default=None, help="tests/tooling dir (default: tests, scripts)")
    parser.add_argument("--skip-index", action="store_true", help="skip the PyPI checks (imports only)")
    parser.add_argument("--skip-imports", action="store_true", help="skip the declared-imports check (PyPI only)")
    parser.add_argument("--frozen-import", action="store_true", help="offline mode (compatibility alias)")
    parser.add_argument("--offline", action="store_true", help="fail closed if committed dependencies changed")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    problems: list[str] = []
    facts: list[str] = []

    try:
        index_checked = False
        if not args.skip_index:
            rng = resolve_range(repo, args.base, args.head, args.fallback_ref)
            prefix = git(repo, "rev-parse", "--show-prefix").stdout.strip()
            lock_name = prefix + "uv.lock"
            old_text = file_at(repo, rng.old, lock_name)
            new_text = file_at(repo, rng.head, lock_name)
            if new_text is None:
                raise GuardError(f"No committed lock at {rng.head}:{lock_name}")
            packages = read_lock(new_text)
            print(f"dependency check: committed {lock_name} compared with {rng.old[:12]}")
            old_oid = git(repo, "rev-parse", "--verify", f"{rng.old}:{lock_name}", check=False)
            new_oid = git(repo, "rev-parse", "--verify", f"{rng.head}:{lock_name}")
            unchanged = old_oid.returncode == 0 and old_oid.stdout == new_oid.stdout
            if unchanged and (args.offline or args.frozen_import or args.index_url == PYPI_API):
                facts.append("Committed lock unchanged; index audit not needed.")
            elif not unchanged and (args.offline or args.frozen_import):
                raise GuardError("Committed lock changed; index audit required but offline mode is enabled.")
            else:
                index_checked = True
            if index_checked:
                old_names = {p.name for p in read_lock(old_text)} if old_text else set()
                new = [p for p in packages if p.name not in old_names]
                index = Index(args.index_url)
                print(
                    f"dependency check: {len(packages)} locked package(s) checked against PyPI, {len(new)} new in this change"
                )
                problems += check_exist(index, packages)
                report = check_new_packages(
                    index, [p for p in new if p.registry == PYPI_REGISTRY], args.min_age_days, datetime.now(UTC)
                )
                problems += report.problems
                facts += report.facts
        if not args.skip_imports:
            problems += check_imports(repo, args.runtime_dir or ["src"], args.dev_dir or ["tests", "scripts"])
    except GuardError as exc:
        print(f"ERROR dependency check could not complete: {exc}", file=sys.stderr)
        return EXIT_ERROR

    for fact in facts:
        print(fact)
    if not problems:
        print("OK dependency checks passed (committed lock and declared imports).")
        return EXIT_OK
    for problem in problems:
        print(f"PROBLEM {problem}")
    print(f"FAIL {len(problems)} dependency problem(s).")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

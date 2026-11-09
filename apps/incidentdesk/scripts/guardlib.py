"""Shared helpers for the CI guard scripts (RPT-02).

Every guard takes a base ref and a head ref and looks at what changed between them, so the same
command runs identically on a laptop and in GitHub Actions. This module holds the git plumbing.

Exit codes used by every guard script:
  0  the check passed (or the change carries owner approval)
  1  the check failed: a rule was broken
  2  the guard could not do its job (bad ref, git missing, ...). Never treated as a pass.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40  # GitHub sends this as the "before" SHA when a branch is pushed for the first time.

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERROR = 2


class GuardError(Exception):
    """The guard could not run (as opposed to the guard running and finding a problem)."""


@dataclass(frozen=True)
class Change:
    """One file-level change. `status` is git's letter: A added, M modified, D deleted, R renamed, T type."""

    status: str
    path: str  # the path after the change (the old path for a deletion)
    old_path: str | None = None  # only for renames


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        # Fixed argument list, no shell: nothing from a ref name or path is ever interpreted by a shell.
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise GuardError("git is not installed or not on PATH.") from exc
    if check and result.returncode != 0:
        raise GuardError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def resolve_commit(repo: Path, ref: str) -> str | None:
    """Return the full commit SHA for `ref`, or None if it does not name a commit in this clone."""
    result = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _empty_tree(repo: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "hash-object", "-t", "tree", "--stdin"],  # noqa: S607
            cwd=repo,
            input="",
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise GuardError("Could not compute git's empty tree.") from exc
    return result.stdout.strip()


@dataclass(frozen=True)
class Range:
    """What the guards compare: files as of `old` (a commit or the empty tree) against `head`."""

    old: str
    head: str
    note: str  # one line saying how the base was chosen, printed so the log is self-explaining


DEFAULT_FALLBACK = "origin/main"


def split_refs(value: str | Sequence[str] | None) -> list[str]:
    """Candidate refs from one string or a list; commas and spaces separate names. None means the default."""
    if value is None:
        return [DEFAULT_FALLBACK]
    values = [value] if isinstance(value, str) else list(value)
    return [name for v in values for name in re.split(r"[,\s]+", v) if name]


def _nearest_fallback(repo: Path, names: list[str], head_sha: str) -> tuple[str, str, str] | None:
    """Among the candidate refs that exist, the one whose merge-base with head is closest to head.

    Closest = fewest commits between the merge-base and head, so a ticket branch cut from an integration
    branch is compared with that branch rather than with an older branch it diverged from long ago.
    A bare name such as "fresh-start" is also tried as "origin/fresh-start" (CI checkouts only have the latter).
    Ties go to the name listed first. Returns (ref name, its commit, merge-base commit) or None.
    """
    best: tuple[int, int, str, str, str] | None = None
    for order, name in enumerate(names):
        sha = resolve_commit(repo, name) or (
            None if name.startswith("origin/") else resolve_commit(repo, f"origin/{name}")
        )
        if sha is None:
            continue
        base = git(repo, "merge-base", sha, head_sha, check=False)
        if base.returncode != 0 or not base.stdout.strip():
            continue  # unrelated histories: no common ancestor to compare from
        merge_base = base.stdout.strip()
        distance = int(git(repo, "rev-list", "--count", f"{merge_base}..{head_sha}").stdout.strip())
        if best is None or (distance, order) < best[:2]:
            best = (distance, order, name, sha, merge_base)
    return None if best is None else best[2:]


def resolve_range(repo: Path, base: str | None, head: str, fallback_refs: str | Sequence[str] | None) -> Range:
    """Work out the (old, head) pair to diff.

    - Normal case: old = merge-base(base, head), so changes that landed on the base branch after this
      branch was cut are not blamed on this branch (the same view GitHub shows for a PR).
    - base missing, all zeros, or not in this clone (first push of a new branch; a force-push that
      replaced the old "before" commit): use the nearest of the fallback refs (see _nearest_fallback).
    - nothing resolvable: compare against the empty tree, so every file counts as new. That fails closed.
    """
    head_sha = resolve_commit(repo, head)
    if head_sha is None:
        raise GuardError(f"The head ref {head!r} does not exist in this repository.")

    if base and base != ZERO_SHA:
        base_sha = resolve_commit(repo, base)
        if base_sha is not None:
            return Range(_merge_base(repo, base_sha, head_sha), head_sha, f"base {base} (merge-base with head)")
        why = f"base {base} is not in this clone"
    else:
        why = "no base given (first push of a branch)"

    names = split_refs(fallback_refs)
    found = _nearest_fallback(repo, names, head_sha)
    if found is not None:
        name, _, old = found
        if old != head_sha:
            return Range(old, head_sha, f"{why}; compared with merge-base of {name}, the nearest of {', '.join(names)}")
        # That branch already contains head (for example the very first push of the branch itself): an empty
        # comparison would pass vacuously, so look at head's own commit instead.
        parent = resolve_commit(repo, f"{head_sha}^")
        if parent is not None:
            return Range(parent, head_sha, f"{why}; {name} already contains head, using head's parent")
    return Range(
        _empty_tree(repo),
        head_sha,
        f"{why}; no fallback found among {', '.join(names) or 'none'}, treating every file as new",
    )


def _merge_base(repo: Path, a: str, b: str) -> str:
    result = git(repo, "merge-base", a, b, check=False)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else a  # unrelated histories: diff against `a` itself


def changed_files(repo: Path, rng: Range, *, find_renames: bool = False) -> list[Change]:
    """Files that differ between rng.old and rng.head.

    With find_renames=False a rename is reported as a delete of the old path plus an add of the new one,
    which is what a path-based guard wants (both ends are checked).
    """
    flag = "-M" if find_renames else "--no-renames"
    out = git(repo, "diff", "--name-status", "-z", flag, rng.old, rng.head).stdout
    parts = out.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[0] in "RC":
            changes.append(Change(status[0], parts[i + 2], parts[i + 1]))
            i += 3
        else:
            changes.append(Change(status[0], parts[i + 1]))
            i += 2
    return changes


def file_at(repo: Path, ref: str, path: str) -> str | None:
    """Text of `path` as of `ref`, or None if it does not exist there (or is not a regular blob)."""
    # Trailing -- prevents Git treating long Windows revision:path values as filenames.
    result = git(repo, "show", f"{ref}:{path}", "--", check=False)
    return result.stdout if result.returncode == 0 else None


def add_common_arguments(parser: argparse.ArgumentParser, *, approval: bool = True) -> None:
    parser.add_argument("--repo", default=".", help="path to the git working copy (default: current directory)")
    parser.add_argument(
        "--base",
        default=None,
        help="base ref or SHA to compare against (a PR's base, or a push's 'before' SHA; may be all zeros)",
    )
    parser.add_argument("--head", default="HEAD", help="head ref or SHA (default: HEAD)")
    parser.add_argument(
        "--fallback-ref",
        action="append",
        default=None,
        help="used when --base is missing, all zeros or unknown (default: origin/main). Repeatable, and one value "
        "may hold several names separated by commas or spaces; the ref whose merge-base with head is nearest wins.",
    )
    if approval:
        parser.add_argument(
            "--approved",
            action="store_true",
            help="the owner-approval label is present (CI works this out; see guard_approval.py)",
        )


def changed_blobs(
    repo: Path, *, staged: bool, base: str | None, head: str, fallback: list[str] | None
) -> list[tuple[str, bytes]]:
    """Read changed files from the index or requested commit, preserving raw bytes."""
    ref = "" if staged else resolve_range(repo, base, head, fallback).head
    if staged:
        names = git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMT", "-z", "--no-renames").stdout.split(
            "\0"
        )
    else:
        rng = resolve_range(repo, base, head, fallback)
        names = [c.path for c in changed_files(repo, rng) if c.status != "D"]
    blobs = []
    for name in filter(None, names):
        result = subprocess.run(  # noqa: S603
            ["git", "show", f"{ref}:{name}", "--"],  # noqa: S607
            cwd=repo,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise GuardError(f"Could not read blob {ref}:{name}")
        if b"\0" not in result.stdout:
            blobs.append((name, result.stdout))
    return blobs

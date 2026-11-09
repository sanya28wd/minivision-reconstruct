#!/usr/bin/env python3
"""Protected-path guard (RPT-02).

Fails when a change touches dataset/truth, dataset/archive, docs/adr, docs/reference or GLOSSARY.md
(CLAUDE.md "Hard guardrails", AGENTS.md "CI is the enforcer"), unless the owner-approval label is on the
PR (pass --approved). Renames are checked at both ends, so moving a file out of a protected folder also fails.

Usage (same in CI and locally):
    python scripts/guard_protected_paths.py --base origin/main --head HEAD [--approved]

Local-only: Owner-Approved: <reason> in the commit message permits truth-only changes.
Only --staged --commit-msg-file PATH at commit-msg honours this trailer.
Range mode (--base/--head), including check_all and accept_merge, requires --approved for protected changes.
CI (CI or GITHUB_ACTIONS set) never honours trailers: only --approved from the CI label counts.

Files that make up the guardrails themselves (.github/, scripts/, .pre-commit-config.yaml) are not
blocked, but are printed as a NOTICE so the owner reads that diff with extra care.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from guardlib import (
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_OK,
    Change,
    GuardError,
    add_common_arguments,
    changed_files,
    git,
    resolve_range,
)

# Directory prefixes (must end with "/") and exact files. Compared case-insensitively because Windows and
# macOS file systems are, so "Dataset/Truth" there is the same folder.
PROTECTED_DIRS = (
    "dataset/truth/",
    "dataset/archive/",
    "docs/adr/",
    "docs/reference/",
    "apps/incidentdesk/docs/adr/",
    "apps/incidentdesk/docs/reference/",
    "apps/incidentdesk/scripts/",
    # Root worker entries are the collaborator's paths.
    "src/minivision_reconstruct/",
    "tests/",
    "evaluation/",
    "configs/",
    "data/",
)
PROTECTED_FILES = (
    "glossary.md",
    "apps/incidentdesk/glossary.md",
    "pyproject.toml",
    "uv.lock",
    "readme.md",
    "implementation_plan.md",
)
# Not blocked, only reported: if these change, the guards themselves may have changed.
WATCHED_PREFIXES = (".github/", "scripts/", ".pre-commit-config.yaml")
MAX_LISTED = 30  # keep the log readable when a whole tree counts as new


def is_protected(path: str) -> bool:
    lowered = path.lower()
    return lowered.startswith(PROTECTED_DIRS) or lowered in PROTECTED_FILES


def is_watched(path: str) -> bool:
    return path.lower().startswith(WATCHED_PREFIXES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--commit-msg-file", type=Path)
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    try:
        if args.staged:
            names = git(repo, "diff", "--cached", "--name-only", "-z", "--no-renames").stdout.split("\0")
            changes = [Change("M", name) for name in names if name]
            comparison = "staged index"
        else:
            rng = resolve_range(repo, args.base, args.head, args.fallback_ref)
            changes = changed_files(repo, rng)
            comparison = f"{rng.old[:12]}..{rng.head[:12]} ({rng.note})"
        message = ""
        if args.staged and args.commit_msg_file and not (os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")):
            message = args.commit_msg_file.read_text(encoding="utf-8")
    except (GuardError, OSError, UnicodeError) as exc:
        print(f"ERROR protected-path guard could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"protected-path guard: comparing {comparison}")
    touched = sorted({c.path for c in changes if is_protected(c.path)})
    watched = sorted({c.path for c in changes if is_watched(c.path)})

    for path in watched:
        print(f"NOTICE guardrail file changed, owner please read this diff: {path}")

    if not touched:
        print("OK no protected path was changed.")
        return EXIT_OK

    for path in touched[:MAX_LISTED]:
        print(f"PROTECTED {path}")
    if len(touched) > MAX_LISTED:
        print(f"PROTECTED ... and {len(touched) - MAX_LISTED} more")
    reasons = re.findall(r"^Owner-Approved:[ \t]*([^\r\n]*)$", message, re.MULTILINE)
    reason = next((value.strip() for value in reasons if value.strip()), "")
    if reason and all(path.lower().startswith("dataset/truth/") for path in touched):
        print(f"OK {len(touched)} protected path(s) owner-approved by trailer: {reason}")
        return EXIT_OK
    if args.approved:
        print(f"OK {len(touched)} protected path(s) changed, but the owner-approval label is present (approved).")
        return EXIT_OK
    print(
        f"FAIL {len(touched)} protected path(s) changed without owner approval. Protected paths are never edited by "
        "hand; if this change is intended, the owner adds the approval label to the PR and CI re-runs."
    )
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

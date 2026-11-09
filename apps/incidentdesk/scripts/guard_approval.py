#!/usr/bin/env python3
"""Owner approval for the protected-path and test-integrity guards (RPT-02).

A guard is waived only by a GitHub label on the pull request, and only if the OWNER applied it and it is
still on. Anyone with triage rights, or an agent holding a token, can add a label, so "the label exists"
is not enough: this script reads the label history (the issue-events API) and checks who applied the
last change to each label.

CI feeds it the history of every PR tied to the commit (the PR itself on a pull_request event; the PR(s)
that contain the pushed commit on a push event; none for a push with no PR):

    gh api --paginate --slurp repos/OWNER/REPO/issues/NUMBER/events > events.json
    python scripts/guard_approval.py --owners owner-login \
        --check protected_paths=owner-approved-protected-paths \
        --check test_changes=owner-approved-test-change \
        --github-output "$GITHUB_OUTPUT" events.json [more-events.json ...]

It writes `<key>=true|false` per --check. If several files are given (several PRs), approval in any one counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from guardlib import EXIT_ERROR, EXIT_OK


def load_events(path: Path) -> list[dict[str, Any]]:
    """Read `gh api --paginate` output: one JSON array, a list of page arrays (--slurp), or arrays back to back."""
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    events: list[dict[str, Any]] = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        value, pos = decoder.raw_decode(text, pos)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, list):
                events.extend(e for e in item if isinstance(e, dict))
            elif isinstance(item, dict):
                events.append(item)
    return events


def is_approved(events: list[dict[str, Any]], label: str, owners: set[str]) -> bool:
    """True if the most recent labeled/unlabeled event for `label` is a "labeled" by one of `owners`."""
    relevant = [
        e for e in events if e.get("event") in ("labeled", "unlabeled") and (e.get("label") or {}).get("name") == label
    ]
    if not relevant:
        return False
    last = max(relevant, key=lambda e: (str(e.get("created_at", "")), int(e.get("id", 0))))
    actor = ((last.get("actor") or {}).get("login") or "").lower()
    return last["event"] == "labeled" and actor in {o.lower() for o in owners}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owners", required=True, help="comma-separated GitHub logins allowed to approve")
    parser.add_argument(
        "--check", action="append", default=[], metavar="KEY=LABEL", help="output KEY=true if LABEL approves"
    )
    parser.add_argument("--github-output", default=None, help="file to append KEY=value lines to ($GITHUB_OUTPUT)")
    parser.add_argument("events", nargs="*", type=Path, help="issue-events JSON files, one per pull request")
    args = parser.parse_args(argv)

    owners = {o.strip() for o in args.owners.split(",") if o.strip()}
    if not owners:
        parser.error("--owners needs at least one login")
    checks: list[tuple[str, str]] = []
    for spec in args.check:
        key, sep, label = spec.partition("=")
        if not sep or not key or not label:
            parser.error(f"--check expects KEY=LABEL, got {spec!r}")
        checks.append((key, label))

    try:
        per_pr = [load_events(p) for p in args.events]
    except (OSError, ValueError) as exc:
        print(f"ERROR cannot read the label history, so nothing is approved: {exc}", file=sys.stderr)
        return EXIT_ERROR

    lines = []
    for key, label in checks:
        approved = any(is_approved(events, label, owners) for events in per_pr)
        print(f"{key}: {'approved' if approved else 'not approved'} (label {label!r}, owners {sorted(owners)})")
        lines.append(f"{key}={'true' if approved else 'false'}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

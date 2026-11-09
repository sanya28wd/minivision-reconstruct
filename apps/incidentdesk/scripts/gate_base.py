"""Validate the reviewed default base without creating or moving owner tags."""

import argparse
from pathlib import Path

from guardlib import GuardError, git, resolve_commit

APPROVAL_TAG = "incidentdesk-import-approved"


def validate(repo: Path, base: str, head: str) -> None:
    if base in {APPROVAL_TAG, "refs/tags/" + APPROVAL_TAG}:
        ref = "refs/tags/" + APPROVAL_TAG
        kind = git(repo, "cat-file", "-t", ref, check=False)
        if kind.returncode or kind.stdout.strip() != "tag":
            raise GuardError(f"{APPROVAL_TAG} must exist as an owner-created annotated tag")
        if git(repo, "merge-base", "--is-ancestor", ref, head, check=False).returncode:
            raise GuardError(f"{APPROVAL_TAG} must be an ancestor of {head}")
    old, new = resolve_commit(repo, base), resolve_commit(repo, head)
    if not old or not new:
        raise GuardError("Gate base and head must resolve to commits")
    common = git(repo, "merge-base", old, new, check=False)
    if common.returncode:
        raise GuardError("Gate base and head must share an ancestor")
    if common.stdout.strip() == new:
        raise GuardError("Gate refuses an empty range (merge-base equals head)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=APPROVAL_TAG)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    try:
        validate(Path("."), args.base, args.head)
    except GuardError as exc:
        print(f"ERROR gate base: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

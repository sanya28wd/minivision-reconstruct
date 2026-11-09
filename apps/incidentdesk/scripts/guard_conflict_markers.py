#!/usr/bin/env python3
"""Check changed text blobs in the index (--staged) or a committed range."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from guardlib import EXIT_ERROR, GuardError, add_common_arguments, changed_blobs

LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser, approval=False)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)
    failed = False
    try:
        blobs = changed_blobs(
            Path(args.repo), staged=args.staged, base=args.base, head=args.head, fallback=args.fallback_ref
        )
        for path, data in blobs:
            lowered = path.lower()
            if lowered.startswith("docs/reference/") or Path(lowered).name in LOCKFILES or lowered.endswith(".lock"):
                continue
            for number, line in enumerate(data.splitlines(), 1):
                if line.startswith((b"<<<<<<< ", b">>>>>>> ")) or line == b"=======":
                    print(f"{path}:{number}: unresolved conflict marker")
                    failed = True

    except GuardError as exc:
        print(f"ERROR text guard could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not failed:
        print("OK changed text files passed.")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())

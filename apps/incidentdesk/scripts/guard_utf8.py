#!/usr/bin/env python3
"""Check changed text blobs in the index (--staged) or a committed range."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from guardlib import EXIT_ERROR, GuardError, add_common_arguments, changed_blobs

TEXT_EXTENSIONS = set(".md .py .json .yaml .yml .toml .sql .html .txt .sh .ps1 .css .js".split())


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
            if Path(path).suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                print(f"{path}: invalid UTF-8 at byte offset {exc.start}")
                failed = True

    except GuardError as exc:
        print(f"ERROR text guard could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not failed:
        print("OK changed text files passed.")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())

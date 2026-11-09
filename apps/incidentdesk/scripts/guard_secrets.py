#!/usr/bin/env python3
"""Secret-scanning guard (RPT-02), for CI and as a pre-commit hook.

Scans every line a change ADDS (not the whole tree, not removed lines) for API keys, tokens and private
keys, using the pattern detectors from the `detect-secrets` package (Yelp, Apache-2.0, pip-installable)
plus one pattern for Google API keys ("AIza" + 35 characters), the shape of a Gemini API key, which
detect-secrets does not have. The noisy "looks random" and "variable is called password" detectors are
left out so that a failure means something.

  * CI:        python scripts/guard_secrets.py --base <PR base or push 'before'> --head <sha>
               Every commit in the range is scanned, plus the net diff, so a key that was added and then
               removed again is still caught: once pushed, it is in history and must be rotated.
  * pre-commit: python scripts/guard_secrets.py --staged      (see .pre-commit-config.yaml)

A finding names the file, line and kind of secret. It NEVER prints the secret itself. There is no inline
"allowlist" comment on purpose: an agent could add one as easily as it adds a key. If a real key leaked,
rotate it; do not just delete the line.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.plugins.base import RegexBasedDetector

from guardlib import (
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_OK,
    GuardError,
    add_common_arguments,
    git,
    resolve_commit,
    resolve_range,
)


class GoogleApiKeyDetector(RegexBasedDetector):
    """Google Cloud / Gemini API key: "AIza" followed by 35 URL-safe characters."""

    secret_type = "Google API Key"  # noqa: S105  (a label, not a secret)
    denylist = (re.compile(r"AIza[0-9A-Za-z_\-]{35}"),)


# Detectors left out because they flag ordinary text: entropy guessers, "password = ..." keywords, public IPs.
NOISY_DETECTORS = frozenset({"Base64HighEntropyString", "HexHighEntropyString", "KeywordDetector", "IPPublicDetector"})


def build_detectors() -> list[Any]:
    classes: dict[str, Any] = get_mapping_from_secret_type_to_class()
    detectors = [cls() for cls in classes.values() if cls.__name__ not in NOISY_DETECTORS]
    detectors.append(GoogleApiKeyDetector())
    return detectors


@dataclass(frozen=True)
class Finding:
    where: str  # a short commit id, "net diff" or "staged"
    path: str
    line: int
    kind: str


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def added_lines(diff_text: str) -> Iterator[tuple[str, int, str]]:
    """Yield (path, line number in the new file, text) for each added line of a `-U0` unified diff.

    Hunk headers say how many lines follow, so a content line that happens to start with "+++" is never
    mistaken for a file header.
    """
    path = ""
    old_left = new_left = 0
    new_line = 0
    for raw in diff_text.split("\n"):
        if old_left <= 0 and new_left <= 0:
            if raw.startswith("+++ "):
                name = raw[4:].split("\t")[0].strip('"')
                path = "" if name == "/dev/null" else name.removeprefix("b/")
            elif (m := _HUNK.match(raw)) is not None:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                new_left = int(m.group(3)) if m.group(3) is not None else 1
                new_line = int(m.group(2))
            continue
        if raw.startswith("+"):
            yield path, new_line, raw[1:]
            new_line += 1
            new_left -= 1
        elif raw.startswith("-"):
            old_left -= 1
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            old_left -= 1
            new_left -= 1


def scan_diff(diff_text: str, where: str, detectors: list[Any]) -> set[Finding]:
    findings: set[Finding] = set()
    for path, line_no, text in added_lines(diff_text):
        for detector in detectors:
            if any(True for _ in detector.analyze_string(text)):
                findings.add(Finding(where, path, line_no, detector.secret_type))
    return findings


def _diff(repo: Path, *args: str) -> str:
    return git(
        repo, "-c", "core.quotepath=off", "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", *args
    ).stdout


def scan_staged(repo: Path, detectors: list[Any]) -> set[Finding]:
    return scan_diff(_diff(repo, "--cached"), "staged", detectors)


def scan_range(
    repo: Path, base: str | None, head: str, fallback_ref: str | list[str] | None, detectors: list[Any]
) -> set[Finding]:
    rng = resolve_range(repo, base, head, fallback_ref)
    print(f"secret guard: scanning {rng.old[:12]}..{rng.head[:12]} ({rng.note})")
    findings = scan_diff(_diff(repo, rng.old, rng.head), "net diff", detectors)

    log_args = [
        "-c",
        "core.quotepath=off",
        "log",
        "-p",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        "--no-merges",
    ]
    log_args += ["--format=@@@commit %H"]
    log_args += [f"{rng.old}..{rng.head}"] if resolve_commit(repo, rng.old) else [rng.head]
    log_text = git(repo, *log_args).stdout
    for chunk in log_text.split("@@@commit ")[1:]:
        sha, _, body = chunk.partition("\n")
        findings |= scan_diff(body, sha.strip()[:12], detectors)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser, approval=False)
    parser.add_argument(
        "--staged", action="store_true", help="scan staged changes instead of a commit range (pre-commit)"
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    detectors = build_detectors()

    try:
        if args.staged:
            print("secret guard: scanning staged changes")
            findings = scan_staged(repo, detectors)
        else:
            findings = scan_range(repo, args.base, args.head, args.fallback_ref, detectors)
    except GuardError as exc:
        print(f"ERROR secret guard could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not findings:
        print("OK no secrets found in added lines.")
        return EXIT_OK
    places: dict[tuple[str, int, str], list[str]] = {}
    for f in sorted(findings, key=lambda f: (f.path, f.line, f.kind, f.where)):
        places.setdefault((f.path, f.line, f.kind), []).append(f.where)
    for (path, line, kind), where in places.items():
        print(f"SECRET {path}:{line} {kind} (in {', '.join(where)}; value hidden)")
    print(
        f"FAIL {len(places)} possible secret(s). Remove the value and ROTATE the credential if it was ever "
        "pushed: deleting the line does not remove it from git history. Use environment variables instead."
    )
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

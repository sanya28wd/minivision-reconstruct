"""Fail app integration changes that touch collaborator-owned paths."""

import argparse
from pathlib import Path

from guardlib import git

PATHS = (
    "src/minivision_reconstruct",
    "scripts",
    "configs",
    "tests",
    "evaluation",
    "data",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "IMPLEMENTATION_PLAN.md",
)


def check(repo: Path, base: str, head: str) -> int:
    # Git's :/ anchor keeps these paths at the repository root from the app cwd.
    paths = [":/" + path for path in PATHS]
    result = git(repo, "diff", "--quiet", f"{base}...{head}", "--", *paths, check=False)
    if result.returncode == 0:
        print("OK collaborator paths unchanged.")
        return 0
    if result.returncode != 1:
        print("ERROR collaborator path comparison failed: " + result.stderr.strip())
        return 2
    changed = git(repo, "diff", "--name-only", f"{base}...{head}", "--", *paths)
    print("FAIL collaborator paths changed:\n" + changed.stdout.strip())
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    return check(Path("."), args.base, args.head)


if __name__ == "__main__":
    raise SystemExit(main())

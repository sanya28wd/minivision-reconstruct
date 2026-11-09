"""A throwaway git repository for testing the CI guard scripts (RPT-02).

The guards compare two git refs, so their tests build real repositories in a temporary directory and
make real commits. Nothing here touches the project repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Isolate from the developer's git config (signing, hooks, templates).
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Sandbox",
    "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
    "GIT_COMMITTER_NAME": "Sandbox",
    "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
}

ZEROS = "0" * 40


class GitSandbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    @classmethod
    def clone(cls, source: GitSandbox, dest: Path) -> GitSandbox:
        """A clone of `source`: its branches exist there only as origin/<name>, like in a CI checkout."""
        subprocess.run(["git", "clone", "-q", str(source.path), str(dest)], env=_ENV, check=True, capture_output=True)
        clone = cls.__new__(cls)
        clone.path = dest
        return clone

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.path, env=_ENV, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def delete(self, rel: str) -> None:
        self.git("rm", "-q", "-f", rel)

    def move(self, old: str, new: str) -> None:
        (self.path / new).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", old, new)

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str) -> None:
        self.git("checkout", "-q", "-b", name)

    def checkout(self, name: str) -> None:
        self.git("checkout", "-q", name)


def build_diverged_history(sandbox: GitSandbox) -> None:
    """The shape of this project's real history, which the first-push fallback has to cope with.

    main        A ---------- M1            (A holds an old test and an old truth file; main moves on)
    fresh-start  \\-- F1                   (the integration branch: F1 deletes the old test, edits truth, adds a key)
    feature              \\-- harmless     (a ticket branch cut from fresh-start; leaves the repo on it)
    """
    sandbox.write("README.md", "hello\n")
    sandbox.write("dataset/truth/old.json", '{"v": 1}\n')
    sandbox.write("tests/test_old.py", "def test_old():\n    assert 1 == 1\n")
    sandbox.commit("A: old state")
    sandbox.branch("fresh-start")
    sandbox.delete("tests/test_old.py")
    sandbox.write("dataset/truth/old.json", '{"v": 2}\n')
    sandbox.write("tests/test_new.py", "def test_new():\n    assert 2 == 2\n")
    sandbox.write("src/leaked.py", 'TOKEN = "' + "ghp_" + "R2d9xK3mQ7vL1nB8cT5yU0zW4eH6jA2sD9fG" + '"\n')
    sandbox.commit("F1: the flush")
    sandbox.checkout("main")
    sandbox.write("README.md", "main moved on\n")
    sandbox.commit("M1")
    sandbox.checkout("fresh-start")
    sandbox.branch("feature")
    sandbox.write("src/harmless.py", "x = 1\n")
    sandbox.commit("a harmless ticket change")

"""Exercise the actual check_all settings preamble in a temporary Git worktree."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from git_sandbox import GitSandbox


@pytest.mark.parametrize("shell", ["bash", "pwsh"] if os.name == "nt" or shutil.which("pwsh") else ["bash"])
def test_check_all_worktree_settings(tmp_path: Path, shell: str) -> None:
    repo = GitSandbox(tmp_path / "main")
    repo.write("README.md", "base\n")
    repo.commit()
    wt = tmp_path / "worktree with spaces"
    repo.git("worktree", "add", "-b", "ticket", str(wt))
    suffix = "sh" if shell == "bash" else "ps1"
    source = (Path(__file__).resolve().parents[1] / "scripts" / f"check_all.{suffix}").read_text(encoding="utf-8")
    prefix = source.split("passed=0" if shell == "bash" else "$script:passed = 0")[0]
    echo = (
        '\nprintf "%s|%s|%s" "$PGPASSWORD" "$TEST_DATABASE_URL" "$INCIDENT_PG_BIN_DIR"\n'
        if shell == "bash"
        else '\nWrite-Output "$env:PGPASSWORD|$env:TEST_DATABASE_URL|$env:INCIDENT_PG_BIN_DIR"\n'
    )
    scripts = wt / "scripts"
    scripts.mkdir()
    script = scripts / f"check_all.{suffix}"
    script.write_text(prefix + echo, encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items() if k not in {"PGPASSWORD", "TEST_DATABASE_URL", "INCIDENT_PG_BIN_DIR"}}
    env["PGPASSWORD"] = "shell-wins"
    repo.write(
        ".env.test",
        "PGPASSWORD=file-value\nTEST_DATABASE_URL=main-url\nINCIDENT_PG_BIN_DIR=main-bin\nIGNORED=$(touch SHOULD_NOT_EXIST)\n",
    )
    exe = shutil.which(shell)
    if shell == "bash" and os.name == "nt":
        git_exe = shutil.which("git")
        if git_exe:
            git_bash = Path(git_exe).resolve().parents[1] / "bin" / "bash.exe"
            if git_bash.is_file():
                exe = str(git_bash)
    assert exe is not None, f"{shell} needed to exercise settings loader"
    command = [exe, str(script)] if shell == "bash" else [exe, "-NoProfile", "-File", str(script)]
    result = subprocess.run(command, cwd=wt, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "shell-wins|main-url|main-bin"
    assert not (wt / "SHOULD_NOT_EXIST").exists()
    (wt / ".env.test").write_text("TEST_DATABASE_URL=local-url\nINCIDENT_PG_BIN_DIR=local-bin\n", encoding="utf-8")
    result = subprocess.run(command, cwd=wt, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "shell-wins|local-url|local-bin"

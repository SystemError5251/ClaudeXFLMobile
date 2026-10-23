"""Self-update: pull the git checkout psygen runs from.

The README installs psygen with ``git clone`` and ``pip install -e .``, so
updating is ``git pull`` in that checkout. pip only runs again when
``pyproject.toml`` changed (new dependencies or commands); code changes
take effect without it, after psygen restarts.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_URL = "https://github.com/SystemError5251/ClaudeXFLMobile.git"


class UpdateError(Exception):
    """The update could not run; the message says what to do instead."""


@dataclass
class UpdateResult:
    updated: bool
    old: str = ""
    new: str = ""
    commits: list[str] = field(default_factory=list)  # one-line summaries, newest first
    reinstalled: bool = False

    def message(self) -> str:
        if not self.updated:
            if self.commits:
                return f"{len(self.commits)} new commits available, run psygen update to get them"
            return f"psygen is up to date ({self.old})"
        text = f"updated {self.old} -> {self.new} ({len(self.commits)} commits)"
        if self.reinstalled:
            text += ", dependencies reinstalled"
        return text + ". Restart psygen to use the new version."


def checkout_root() -> Path | None:
    """The git checkout this package was installed from, if any."""
    root = Path(__file__).resolve().parents[2]  # src/psygen/update.py -> repo
    return root if (root / ".git").exists() and (root / "pyproject.toml").exists() else None


def _git(root: Path, *args: str, timeout: float = 120) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"git {args[0]} took too long (no internet?)") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise UpdateError(f"git {args[0]} failed: {detail[-1] if detail else 'no details'}")
    return proc.stdout.strip()


def update(
    root: Path | None = None,
    check_only: bool = False,
    report: Callable[[str], None] | None = None,
) -> UpdateResult:
    """Fetch and fast-forward the checkout. Raises :class:`UpdateError`."""
    say = report or (lambda _msg: None)
    root = root or checkout_root()
    if root is None:
        raise UpdateError(
            "psygen was not installed from a git checkout. Reinstall with:\n"
            f"  git clone {REPO_URL}\n  cd ClaudeXFLMobile && pip install -e ."
        )
    if shutil.which("git") is None:
        raise UpdateError("git is not installed (pkg install git)")

    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(
            f"{root} has local changes, so it is not updated automatically. "
            "Keep them with `git stash` (or drop them with `git checkout .`), then update again."
        )
    try:
        _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except UpdateError as exc:
        raise UpdateError("the checkout's branch doesn't track a remote branch (try `git checkout main`)") from exc

    say("checking for updates")
    _git(root, "fetch", "--quiet")
    old = _git(root, "rev-parse", "--short", "HEAD")
    log = _git(root, "log", "--format=%h %s", "HEAD..@{u}")
    result = UpdateResult(False, old=old, new=old, commits=log.splitlines() if log else [])
    if not result.commits or check_only:
        return result

    changed = _git(root, "diff", "--name-only", "HEAD", "@{u}").splitlines()
    say(f"pulling {len(result.commits)} new commits")
    try:
        _git(root, "merge", "--ff-only", "--quiet", "@{u}")
    except UpdateError as exc:
        raise UpdateError(
            "the local branch has its own commits, so it can't simply move forward. "
            f"Update by hand in {root} (git pull)."
        ) from exc
    result.updated = True
    result.new = _git(root, "rev-parse", "--short", "HEAD")

    if "pyproject.toml" in changed:
        say("dependencies changed, running pip install")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-e", str(root)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no details"]
            raise UpdateError(f"code updated, but pip install failed: {tail[0]}. Run `pip install -e {root}`.")
        result.reinstalled = True
    return result

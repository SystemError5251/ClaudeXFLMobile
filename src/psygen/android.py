"""Android helpers for Termux: open FL Studio Mobile, notify, keep running.

Everything here is best effort and quiet when it doesn't apply: off Termux,
or without the Termux:API app (``termux-notification``), the calls simply
return False.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

FLM_PACKAGE = "com.imageline.FLM"


def _run(args: list[str], timeout: float = 15) -> bool:
    if shutil.which(args[0]) is None:
        return False
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # `am` reports some failures on stdout with exit code 0.
    return proc.returncode == 0 and "Error" not in (proc.stdout + proc.stderr)


def open_flm(package: str = FLM_PACKAGE) -> bool:
    """Bring FL Studio Mobile to the front (start it if needed)."""
    if not config.is_termux():
        return False
    launcher = (f"intent:#Intent;action=android.intent.action.MAIN;"
                f"category=android.intent.category.LAUNCHER;package={package};launchFlags=0x10200000;end")
    return _run(["am", "start", "--user", "0", launcher]) or _run(["am", "start", launcher])


def notify(title: str, text: str) -> bool:
    """An Android notification (needs the Termux:API app and `pkg install termux-api`)."""
    if not config.is_termux():
        return False
    return _run(["termux-notification", "--id", "psygen", "--title", title, "--content", text])


def wake_lock(on: bool) -> bool:
    """Keep Termux running while the screen is off or another app is open."""
    if not config.is_termux():
        return False
    return _run(["termux-wake-lock" if on else "termux-wake-unlock"])


def background_log() -> Path:
    return config.cache_dir() / "background.log"


def start_background(argv: list[str]) -> Path:
    """Run ``psygen <argv>`` detached from the terminal; output goes to the log.
    Returns the log path."""
    log = background_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as out:
        subprocess.Popen(
            [sys.executable, "-m", "psygen", *argv],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, env=dict(os.environ, PSYGEN_BACKGROUND="1"),
        )
    return log

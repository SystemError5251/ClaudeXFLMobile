"""User configuration and well-known locations (Termux / Android aware)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or Path(
        "/data/data/com.termux"
    ).is_dir()


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "psygen"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "psygen"


def config_path() -> Path:
    return config_dir() / "config.json"


def index_path() -> Path:
    return cache_dir() / "library.json"


# Places where samples commonly live on Android. `~/storage/shared` only exists
# after running `termux-setup-storage`. FL Studio Mobile keeps user samples in
# "FL Studio Mobile/My Samples" on older Android versions; on Android 11+ its
# app folder under Android/data is not readable by Termux.
SHARED_STORAGE_ROOTS = (
    Path.home() / "storage" / "shared",
    Path("/storage/emulated/0"),
    Path("/sdcard"),
)

SAMPLE_SUBDIRS = (
    "Samples",
    "samples",
    "Music/Samples",
    "Download/Samples",
    "FL Studio Mobile/My Samples",
    "Android/data/com.imageline.FLM/files/My Samples",
)


def default_sample_dirs() -> list[Path]:
    """Existing sample folders found in the usual Android locations."""
    found: list[Path] = []
    seen: set[Path] = set()
    for root in SHARED_STORAGE_ROOTS:
        for sub in SAMPLE_SUBDIRS:
            candidate = root / sub
            try:
                if not candidate.is_dir():
                    continue
                real = candidate.resolve()
            except OSError:
                continue
            if real not in seen:
                seen.add(real)
                found.append(candidate)
    return found


def flm_output_dir() -> Path | None:
    """FL Studio Mobile's user folder if Termux can reach it."""
    for root in SHARED_STORAGE_ROOTS:
        candidate = root / "FL Studio Mobile"
        if candidate.is_dir():
            return candidate
    return None


@dataclass
class Config:
    sample_dirs: list[str] = field(default_factory=list)
    analyze_audio: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    def add_sample_dir(self, directory: str | Path) -> bool:
        resolved = str(Path(directory).expanduser())
        if resolved in self.sample_dirs:
            return False
        self.sample_dirs.append(resolved)
        return True

    def resolved_sample_dirs(self) -> list[Path]:
        """Configured folders, or the auto-detected defaults when none are set."""
        if self.sample_dirs:
            return [Path(d).expanduser() for d in self.sample_dirs]
        return default_sample_dirs()

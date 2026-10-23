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


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "psygen"


def tracks_dir() -> Path:
    """Composed tracks (JSON) waiting for export."""
    return data_dir() / "tracks"


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


def default_export_dir() -> Path:
    """Where exports go unless ``export_dir`` is set.

    FL Studio Mobile's own folder when Termux can reach it (``My Samples``
    shows up in its sample browser), else ``Music/psygen`` in shared storage
    (importable from FL Studio Mobile's file browser), else a local folder.
    """
    flm = flm_output_dir()
    if flm is not None:
        return flm / "My Samples" / "psygen"
    for root in SHARED_STORAGE_ROOTS:
        try:
            if root.is_dir() and os.access(root, os.W_OK):
                return root / "Music" / "psygen"
        except OSError:
            continue
    return data_dir() / "exports"


@dataclass
class Config:
    sample_dirs: list[str] = field(default_factory=list)
    analyze_audio: bool = True
    claude_model: str = "claude-opus-5-5"
    compose_effort: str = "high"
    export_dir: str = ""  # empty: default_export_dir()
    export_sample_rate: int = 44100
    export_bits: int = 16

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

    def resolved_export_dir(self) -> Path:
        return Path(self.export_dir).expanduser() if self.export_dir else default_export_dir()

    def resolved_sample_dirs(self) -> list[Path]:
        """Configured folders, or the auto-detected defaults when none are set."""
        if self.sample_dirs:
            return [Path(d).expanduser() for d in self.sample_dirs]
        return default_sample_dirs()

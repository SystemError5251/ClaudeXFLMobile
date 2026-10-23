"""Scan sample folders into a categorized, cached library."""

from __future__ import annotations

import json
import os
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import analysis
from .audio_io import AudioLoadError, numpy_available, wav_duration
from .categorize import Category, categorize_name

AUDIO_EXTENSIONS = frozenset({".wav", ".flac", ".ogg", ".mp3", ".aif", ".aiff", ".m4a", ".opus"})

# Folders that never hold user samples but can be huge or unreadable.
SKIP_DIRS = frozenset({"android", ".thumbnails", ".trash", "lost+found", "__pycache__", "node_modules"})

INDEX_VERSION = 1


@dataclass
class Sample:
    path: str
    category: Category
    confidence: float
    source: str  # "name", "audio" or "none"
    is_loop: bool = False
    bpm: float | None = None
    key: str | None = None
    tags: list[str] = field(default_factory=list)
    duration: float | None = None
    features: dict | None = None
    size: int = 0
    mtime: float = 0.0

    @property
    def name(self) -> str:
        return Path(self.path).name

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Sample":
        d = dict(d)
        try:
            d["category"] = Category(d.get("category", "unknown"))
        except ValueError:
            d["category"] = Category.UNKNOWN
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def iter_audio_files(root: Path) -> Iterator[Path]:
    """Yield audio files below ``root``, skipping hidden and system folders.

    ``root`` itself may be a symlink (``~/storage/shared`` is one); links
    below it are not followed, which avoids loops on Android storage.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                yield Path(dirpath) / name


def build_sample(path: Path, root: Path | None, analyze: bool) -> Sample:
    stat = path.stat()
    guess = categorize_name(path, root)
    sample = Sample(
        path=str(path),
        category=guess.category,
        confidence=guess.confidence,
        source="name" if guess.category is not Category.UNKNOWN else "none",
        is_loop=bool(guess.is_loop),
        bpm=guess.bpm,
        key=guess.key,
        tags=guess.tags,
        size=stat.st_size,
        mtime=stat.st_mtime,
    )
    if path.suffix.lower() == ".wav":
        sample.duration = wav_duration(path)

    if analyze and numpy_available():
        try:
            feats = analysis.analyze_file(path, duration=sample.duration)
        except (AudioLoadError, ValueError, MemoryError):
            feats = None
        if feats is not None:
            sample.features = feats.to_dict()
            if sample.duration is None:
                # Analysis only decodes the first few seconds; anything that
                # filled the window is at least that long.
                sample.duration = feats.duration
            if guess.is_loop is None:
                sample.is_loop = analysis.looks_like_loop(feats)
            if sample.category is Category.UNKNOWN:
                sample.category, sample.confidence = analysis.classify_features(feats)
                sample.source = "audio"
    return sample


class SampleLibrary:
    def __init__(self, samples: Iterable[Sample] = ()):
        self.samples: list[Sample] = list(samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self) -> Iterator[Sample]:
        return iter(self.samples)

    # -- building -----------------------------------------------------------

    @classmethod
    def scan(
        cls,
        roots: Iterable[str | Path],
        analyze: bool = True,
        previous: "SampleLibrary | None" = None,
        progress: Callable[[int, Path], None] | None = None,
    ) -> "SampleLibrary":
        """Scan ``roots`` recursively.

        Files unchanged since ``previous`` (same size and mtime) are reused
        without re-reading the audio, so rescans of a big library are quick.
        """
        cache = {s.path: s for s in previous} if previous else {}
        samples: list[Sample] = []
        seen: set[str] = set()
        for root in roots:
            root = Path(root).expanduser()
            if not root.is_dir():
                continue
            for path in iter_audio_files(root):
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                old = cache.get(key)
                if (
                    old is not None
                    and old.size == stat.st_size
                    and old.mtime == stat.st_mtime
                    and (old.features is not None or not analyze or not numpy_available())
                ):
                    samples.append(old)
                else:
                    try:
                        samples.append(build_sample(path, root, analyze))
                    except OSError:
                        continue
                if progress:
                    progress(len(samples), path)
        return cls(samples)

    # -- persistence --------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": INDEX_VERSION, "samples": [s.to_dict() for s in self.samples]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SampleLibrary":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if payload.get("version") != INDEX_VERSION:
            return cls()
        return cls(Sample.from_dict(d) for d in payload.get("samples", []))

    # -- querying -----------------------------------------------------------

    def by_category(self, category: Category | str, loops: bool | None = None) -> list[Sample]:
        category = Category(category)
        return [
            s for s in self.samples
            if s.category is category and (loops is None or s.is_loop == loops)
        ]

    def counts(self) -> dict[Category, int]:
        out: dict[Category, int] = {}
        for s in self.samples:
            out[s.category] = out.get(s.category, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0].value)))

    def search(self, text: str) -> list[Sample]:
        needle = text.lower()
        return [s for s in self.samples if needle in s.path.lower()]

    def pick(
        self,
        category: Category | str,
        rng: random.Random | None = None,
        loops: bool | None = False,
        min_confidence: float = 0.0,
    ) -> Sample | None:
        """Random sample of a category, weighted toward confident matches."""
        rng = rng or random.Random()
        pool = [s for s in self.by_category(category, loops) if s.confidence >= min_confidence]
        if not pool:
            return None
        weights = [0.2 + s.confidence for s in pool]
        return rng.choices(pool, weights=weights, k=1)[0]

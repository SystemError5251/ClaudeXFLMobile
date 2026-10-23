"""The work behind the UI: settings, background jobs and the generate pipeline.

Nothing here touches curses, so it runs (and is tested) without a terminal.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from .. import ai, config
from ..compose import GENRES, ComposeRequest, Track, compose, skeleton
from ..compose.theory import NOTE_NAMES, SCALES
from ..export import ExportOptions, ExportResult, export_track, slugify
from ..samples import SampleLibrary
from ..samples.audio_io import numpy_available

COMPOSERS = ("claude-high", "claude-medium", "claude-low", "offline")
OUTPUTS = ("all", "preview", "midi")  # stems + preview, preview only, MIDI only


@dataclass
class Settings:
    """What the main screen collects. Empty strings mean "Claude decides"."""

    genre: str = "psytrance"
    bpm: str = ""
    key: str = ""
    scale: str = ""
    minutes: str = ""
    idea: str = ""
    composer: str = "claude-high"
    output: str = "all"
    mp3: bool = False

    @classmethod
    def from_config(cls, cfg: config.Config) -> "Settings":
        effort = cfg.compose_effort if cfg.compose_effort in ("high", "medium", "low") else "high"
        settings = cls(composer=f"claude-{effort}")
        for f in fields(cls):
            if f.name in cfg.ui and isinstance(cfg.ui[f.name], type(getattr(settings, f.name))):
                setattr(settings, f.name, cfg.ui[f.name])
        if settings.genre not in GENRES:
            settings.genre = "psytrance"
        if settings.key not in ("", *NOTE_NAMES):
            settings.key = ""
        if settings.scale not in ("", *SCALES):
            settings.scale = ""
        if settings.composer not in COMPOSERS:
            settings.composer = "claude-high"
        if settings.output not in OUTPUTS:
            settings.output = "all"
        return settings

    def remember(self, cfg: config.Config) -> None:
        cfg.ui = asdict(self)
        if self.composer != "offline":
            cfg.compose_effort = self.composer.split("-", 1)[1]

    @property
    def offline(self) -> bool:
        return self.composer == "offline"

    def validate(self) -> list[str]:
        """Problems with the typed-in values, as messages for the user."""
        problems = []
        lo, hi = GENRES[self.genre].bpm_range
        bpm = _number(self.bpm)
        if self.bpm.strip() and (bpm is None or not 60 <= bpm <= 220):
            problems.append(f"BPM should be a number like {GENRES[self.genre].default_bpm} (genre range {lo}-{hi})")
        minutes = _number(self.minutes)
        if self.minutes.strip() and (minutes is None or not 1 <= minutes <= 30):
            problems.append("Length should be minutes between 1 and 30, like 8")
        return problems

    def request(self, seed: int | None = None) -> ComposeRequest:
        return ComposeRequest(
            genre=self.genre,
            idea=self.idea.strip(),
            bpm=_number(self.bpm),
            key=self.key or None,
            scale=self.scale or None,
            minutes=_number(self.minutes),
            seed=seed,
        )

    def export_options(self, cfg: config.Config) -> ExportOptions:
        return ExportOptions(
            sample_rate=cfg.export_sample_rate,
            bits=cfg.export_bits,
            stems=self.output == "all",
            preview=self.output in ("all", "preview"),
            mp3=self.mp3 and self.output != "midi",
        )


def _number(text: str) -> float | None:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


# -- background jobs ---------------------------------------------------------


class Cancelled(Exception):
    pass


Report = Callable[[str], None]


class Job:
    """Runs ``fn(report)`` in a thread. The UI polls :meth:`poll` for news.

    ``report`` raises :class:`Cancelled` once :meth:`cancel` was called, so
    long work stops at its next progress message. A Claude request that is
    already on its way can't be interrupted; its answer is then ignored.
    """

    def __init__(self, title: str, fn: Callable[[Report], Any]):
        self.title = title
        self._fn = fn
        self._events: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self.log: list[str] = []
        self.state = "running"  # running, done, failed, cancelled
        self.result: Any = None
        self.error = ""
        self.thread = threading.Thread(target=self._run, name=f"psygen-{title}", daemon=True)

    def start(self) -> "Job":
        self.thread.start()
        return self

    def report(self, message: str) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        self._events.put(("log", message))

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set() and self.state == "running"

    def _run(self) -> None:
        try:
            result = self._fn(self.report)
        except Cancelled:
            self._events.put(("cancelled", None))
        except ai.ClaudeError as exc:
            self._events.put(("failed", f"Claude: {exc}"))
        except (OSError, ValueError) as exc:
            self._events.put(("failed", str(exc) or type(exc).__name__))
        except Exception as exc:  # noqa: BLE001 - shown to the user, details in the log file
            _write_crash_log()
            self._events.put(("failed", f"unexpected {type(exc).__name__}: {exc} (details in {crash_log_path()})"))
        else:
            if self._cancel.is_set():
                self._events.put(("cancelled", None))
            else:
                self._events.put(("done", result))

    def poll(self) -> bool:
        """Take in new events; True when something changed."""
        changed = False
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                return changed
            changed = True
            if kind == "log":
                self.log.append(payload)
            elif kind == "done":
                self.state, self.result = "done", payload
            elif kind == "cancelled":
                self.state = "cancelled"
            else:
                self.state, self.error = "failed", payload

    def wait(self, timeout: float | None = None) -> "Job":
        """Block until finished (for tests and scripts)."""
        self.thread.join(timeout)
        self.poll()
        return self


def crash_log_path() -> Path:
    return config.cache_dir() / "ui-error.log"


def _write_crash_log() -> None:
    try:
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(traceback.format_exc(), encoding="utf-8")
    except OSError:
        pass


# -- the pipeline ------------------------------------------------------------


def scan_library(cfg: config.Config, report: Report, use_claude: bool = False) -> SampleLibrary:
    roots = cfg.resolved_sample_dirs()
    if not roots:
        raise ValueError(config.no_sample_dirs_hint())
    analyze = cfg.analyze_audio and numpy_available()
    if cfg.analyze_audio and not analyze:
        report("numpy missing, sorting samples by name only (pkg install python-numpy)")
    for root in roots:
        report(f"scanning {root}")
    last = [0]

    def progress(count: int, path: Path) -> None:
        if count - last[0] >= 50:
            last[0] = count
            report(f"{count} samples ...")

    previous = SampleLibrary.load(config.index_path())
    library = SampleLibrary.scan(roots, analyze=analyze, previous=previous, progress=progress)
    library.save(config.index_path())
    report(f"{len(library)} samples in the library")
    if use_claude:
        weak = [s for s in library if s.source in ("none", "audio")]
        if weak:
            report(f"asking Claude about {len(weak)} unclear samples")
            changed = ai.classify_samples(weak, model=cfg.claude_model)
            library.save(config.index_path())
            report(f"Claude sorted {changed} samples")
    return library


@dataclass
class Outcome:
    track: Track
    track_path: Path
    fixes: list[str] = field(default_factory=list)
    export: ExportResult | None = None
    library: SampleLibrary | None = None  # set when the job (re)scanned


def generate(
    settings: Settings,
    cfg: config.Config,
    library: SampleLibrary,
    report: Report,
    client=None,
) -> Outcome:
    """Scan if the library is empty, compose, save, export: the whole run."""
    scanned = None
    if not len(library) and cfg.resolved_sample_dirs():
        report("sample library is empty, scanning first")
        library = scanned = scan_library(cfg, report)
    if not len(library):
        report("no samples found: parts use the built-in sounds")
    request = settings.request()
    if settings.offline:
        report(f"building a {request.genre} template track (offline)")
        result = skeleton(request, library)
    else:
        effort = settings.composer.split("-", 1)[1]
        report(f"Claude is composing a {request.genre} track ({effort} effort, takes a minute or two)")
        result = compose(request, library, client=client, model=cfg.claude_model, effort=effort)
    track = result.track
    minutes, seconds = divmod(round(track.duration_seconds), 60)
    report(f'composed "{track.title}": {track.bpm:g} BPM, {track.key}, {minutes}:{seconds:02d}')
    if result.fixes:
        report(f"{len(result.fixes)} rule fixes applied")
    track_path = track.save(config.tracks_dir() / f"{slugify(track.title)}.json")
    report(f"saved {track_path.name}")
    outcome = Outcome(track, track_path, result.fixes, library=scanned)
    outcome.export = export_saved(track, settings, cfg, library, report)
    return outcome


def export_saved(
    track: Track,
    settings: Settings,
    cfg: config.Config,
    library: SampleLibrary,
    report: Report,
) -> ExportResult:
    root = cfg.resolved_export_dir()
    report(f"exporting to {root}")
    return export_track(track, root, settings.export_options(cfg), library=library, progress=report)


def saved_tracks() -> list[Path]:
    """Composed track files, newest first."""
    try:
        paths = list(config.tracks_dir().glob("*.json"))
    except OSError:
        return []
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def summary(outcome_or_export: Outcome | ExportResult, track: Track | None = None) -> list[tuple[str, str]]:
    """Result lines (text, style) for the finished-job screen."""
    if isinstance(outcome_or_export, Outcome):
        track, export, fixes = outcome_or_export.track, outcome_or_export.export, outcome_or_export.fixes
    else:
        export, fixes = outcome_or_export, []
    lines: list[tuple[str, str]] = []
    if track is not None:
        minutes, seconds = divmod(round(track.duration_seconds), 60)
        lines.append((track.title, "title"))
        lines.append((f"{track.genre}, {track.bpm:g} BPM, {track.key}, {track.total_bars} bars, "
                      f"{minutes}:{seconds:02d}", "normal"))
        if track.notes:
            lines.append((track.notes, "dim"))
    if export is not None:
        lines.append(("", "normal"))
        lines.append(("Files are in:", "normal"))
        lines.append((str(export.folder), "ok"))
        lines.append((f"  {export.full_midi.name}", "normal"))
        lines.append((f"  midi/  {len(export.part_midi)} part MIDI files", "normal"))
        if export.stems:
            lines.append((f"  stems/ {len(export.stems)} WAV stems", "normal"))
        for extra in (export.preview, export.mp3):
            if extra is not None:
                lines.append((f"  {extra.name}", "normal"))
        lines.append(("  info.txt  (tempo, arrangement, import tips)", "normal"))
        for warning in export.warnings:
            lines.append((f"! {warning}", "warn"))
    if fixes:
        lines.append(("", "normal"))
        lines.append((f"Rule fixes ({len(fixes)}):", "dim"))
        lines.extend((f"  - {fix}", "dim") for fix in fixes)
    return lines

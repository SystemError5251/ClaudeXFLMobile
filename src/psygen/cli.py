"""Command line entry point. Without a command, psygen opens the terminal UI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, ai, config
from .compose import GENRES, ComposeRequest, Track, compose, skeleton
from .compose.theory import SCALES
from .export import ExportOptions, export_track, slugify
from .samples import Category, SampleLibrary
from .samples.audio_io import numpy_available


def _cmd_scan(args: argparse.Namespace) -> int:
    cfg = config.Config.load()
    if args.dirs and args.add:
        for d in args.dirs:
            cfg.add_sample_dir(d)
        print(f"Saved sample folders to {cfg.save()}")
    roots = [Path(d).expanduser() for d in args.dirs] if args.dirs else cfg.resolved_sample_dirs()
    if not roots:
        print(config.no_sample_dirs_hint() + "\nOr pass a folder: psygen scan PATH --add", file=sys.stderr)
        return 1
    analyze = cfg.analyze_audio and not args.no_analysis
    if analyze and not numpy_available():
        print("numpy not installed: categorizing by name only (pkg install python-numpy)")
        analyze = False

    def progress(count: int, path: Path) -> None:
        if sys.stdout.isatty():
            print(f"\r{count} samples  {path.name[:50]:<50}", end="", flush=True)

    for root in roots:
        print(f"Scanning {root}")
    previous = SampleLibrary.load(config.index_path())
    lib = SampleLibrary.scan(roots, analyze=analyze, previous=previous, progress=progress)
    if sys.stdout.isatty():
        print()
    lib.save(config.index_path())
    if args.ai:
        weak = [s for s in lib if s.source in ("none", "audio")]
        if weak:
            print(f"Asking Claude about {len(weak)} unclear samples...")
            try:
                changed = ai.classify_samples(weak, model=cfg.claude_model)
            except ai.ClaudeError as exc:
                print(f"Claude step skipped: {exc}", file=sys.stderr)
            else:
                print(f"Claude categorized {changed} samples")
                lib.save(config.index_path())
    _print_counts(lib)
    return 0


def _print_counts(lib: SampleLibrary) -> None:
    print(f"{len(lib)} samples")
    for category, count in lib.counts().items():
        loops = sum(1 for s in lib.by_category(category) if s.is_loop)
        print(f"  {category.value:<11} {count:>5}" + (f"  ({loops} loops)" if loops else ""))


def _cmd_list(args: argparse.Namespace) -> int:
    lib = SampleLibrary.load(config.index_path())
    if not len(lib):
        print("Library is empty. Run `psygen scan` first.", file=sys.stderr)
        return 1
    if args.category is None and args.search is None:
        _print_counts(lib)
        return 0
    samples = lib.by_category(args.category) if args.category else list(lib)
    if args.search:
        needle = args.search.lower()
        samples = [s for s in samples if needle in s.path.lower()]
    for s in samples:
        extra = " ".join(
            x for x in (
                "loop" if s.is_loop else "",
                f"{s.bpm:g}bpm" if s.bpm else "",
                s.key or "",
                f"{s.duration:.2f}s" if s.duration else "",
            ) if x
        )
        print(f"{s.category.value:<11} {s.confidence:.2f} {s.source:<6} {s.path}  {extra}")
    return 0


def _cmd_dirs(args: argparse.Namespace) -> int:
    cfg = config.Config.load()
    print("Configured:" if cfg.sample_dirs else "Auto-detected (nothing configured):")
    for d in cfg.resolved_sample_dirs():
        print(f"  {d}")
    flm = config.flm_output_dir()
    print(f"FL Studio Mobile folder: {flm or 'not reachable'}")
    print(f"Exports go to: {cfg.resolved_export_dir()}")
    print(f"Termux: {'yes' if config.is_termux() else 'no'}")
    print(f"Shared storage: {'readable' if config.storage_ready() else 'not readable (run termux-setup-storage)'}")
    return 0


def print_track(track: Track) -> None:
    minutes, seconds = divmod(round(track.duration_seconds), 60)
    print(f"{track.title}  ({track.genre}, {track.bpm:g} BPM, {track.key}, "
          f"{track.total_bars} bars, {minutes}:{seconds:02d})")
    if track.notes:
        print(f"  {track.notes}")
    print("Arrangement:")
    for s in track.sections:
        roles = " ".join(r.value for r in track.section_patterns(s))
        print(f"  bar {s.start_bar + 1:>3}  {s.kind.value:<9} {s.bars:>3} bars  "
              f"energy {s.energy:>2}  {s.name}: {roles}")
    print("Parts:")
    for part in track.parts.values():
        count = sum(1 for _ in track.timeline(part.role))
        source = Path(part.sample).name if part.sample else "synth / MIDI only"
        print(f"  {part.role.value:<9} {count:>5} notes  {source}")


def _cmd_compose(args: argparse.Namespace) -> int:
    cfg = config.Config.load()
    request = ComposeRequest(
        genre=args.genre, idea=args.idea or "", bpm=args.bpm, key=args.key,
        scale=args.scale, minutes=args.minutes, seed=args.seed,
    )
    lib = SampleLibrary.load(config.index_path())
    if not len(lib):
        print("No sample library yet (run `psygen scan`); parts will be MIDI only.")
    if args.offline:
        result = skeleton(request, lib)
    else:
        print(f"Claude is composing a {request.profile().name} track...")
        try:
            client = ai.make_client(notice=lambda msg: print(f"  {msg}"), cfg=cfg)
            result = compose(request, lib, client=client, model=cfg.claude_model, effort=cfg.compose_effort)
        except ai.ClaudeError as exc:
            print(f"Composing failed: {exc}\nUse --offline for a template track.", file=sys.stderr)
            return 1
    track = result.track
    print_track(track)
    if result.fixes:
        print(f"Rule fixes ({len(result.fixes)}):")
        for fix in result.fixes:
            print(f"  - {fix}")
    out = Path(args.out) if args.out else config.tracks_dir() / f"{slugify(track.title)}.json"
    print(f"Saved {track.save(out)}")
    if args.export:
        return _export(track, cfg, lib, args)
    return 0


def _latest_track() -> Path | None:
    tracks = sorted(config.tracks_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    return tracks[-1] if tracks else None


def _export(track: Track, cfg: config.Config, lib: SampleLibrary, args: argparse.Namespace) -> int:
    options = ExportOptions(
        sample_rate=args.sample_rate or cfg.export_sample_rate,
        bits=args.bits or cfg.export_bits,
        stems=not (args.midi_only or args.no_stems),
        preview=not (args.midi_only or args.no_preview),
        mp3=args.mp3,
    )
    root = Path(args.dest).expanduser() if args.dest else cfg.resolved_export_dir()
    print(f"Exporting to {root}")

    def progress(msg: str) -> None:
        print(f"  {msg}", flush=True)

    try:
        result = export_track(track, root, options, library=lib, progress=progress)
    except (OSError, ValueError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"  ! {warning}")
    print(f"Done: {result.folder}")
    print(f"  {result.full_midi.name} and {len(result.part_midi)} part MIDI files in midi/")
    if result.stems:
        print(f"  {len(result.stems)} WAV stems in stems/")
    for extra in (result.preview, result.mp3):
        if extra:
            print(f"  {extra.name}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else _latest_track()
    if path is None:
        print("No composed tracks yet. Run `psygen compose` first.", file=sys.stderr)
        return 1
    try:
        track = Track.load(path)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot read {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Exporting {track.title} ({path.name})")
    return _export(track, config.Config.load(), SampleLibrary.load(config.index_path()), args)


def _add_export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dest", help="export folder (default: FL Studio Mobile's folder, see `psygen dirs`)")
    p.add_argument("--midi-only", action="store_true", help="skip all audio")
    p.add_argument("--no-stems", action="store_true", help="only the preview mixdown")
    p.add_argument("--no-preview", action="store_true", help="only the stems")
    p.add_argument("--mp3", action="store_true", help="also write the preview as MP3 (needs ffmpeg)")
    p.add_argument("--sample-rate", type=int, choices=(22050, 44100, 48000))
    p.add_argument("--bits", type=int, choices=(16, 24))


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        track = Track.load(args.path)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot read {args.path}: {exc}", file=sys.stderr)
        return 1
    print_track(track)
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    from .update import UpdateError, update

    try:
        result = update(check_only=args.check, report=lambda msg: print(msg, flush=True))
    except UpdateError as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    for line in result.commits[:20]:
        print(f"  {line}")
    print(result.message())
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    from .ui import run

    return run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="psygen", description=__doc__)
    parser.add_argument("--version", action="version", version=f"psygen {__version__}")
    sub = parser.add_subparsers(dest="command")

    ui = sub.add_parser("ui", help="terminal UI: pick genre and settings, generate and export (the default)")
    ui.set_defaults(func=_cmd_ui)

    scan = sub.add_parser("scan", help="scan sample folders and build the library index")
    scan.add_argument("dirs", nargs="*", help="folders to scan (default: configured or auto-detected)")
    scan.add_argument("--add", action="store_true", help="remember the given folders")
    scan.add_argument("--no-analysis", action="store_true", help="categorize by name only")
    scan.add_argument("--ai", action="store_true", help="ask Claude to categorize samples the name doesn't explain")
    scan.set_defaults(func=_cmd_scan)

    lst = sub.add_parser("list", help="show library contents")
    lst.add_argument("-c", "--category", choices=[c.value for c in Category])
    lst.add_argument("-s", "--search", help="filter by text in the path")
    lst.set_defaults(func=_cmd_list)

    comp = sub.add_parser("compose", help="let Claude compose a track (saved as JSON for export)")
    comp.add_argument("-g", "--genre", default="psytrance", choices=list(GENRES))
    comp.add_argument("-i", "--idea", help="mood, story or references for Claude")
    comp.add_argument("--bpm", type=float)
    comp.add_argument("--key", help="root note, e.g. F# or Am")
    comp.add_argument("--scale", choices=list(SCALES))
    comp.add_argument("--minutes", type=float, help="approximate length")
    comp.add_argument("--seed", type=int, help="seed for sample choices")
    comp.add_argument("--offline", action="store_true", help="template track without Claude")
    comp.add_argument("-o", "--out", help="output JSON path")
    comp.add_argument("--export", action="store_true", help="export MIDI and WAV right after composing")
    _add_export_args(comp)
    comp.set_defaults(func=_cmd_compose)

    exp = sub.add_parser("export", help="write MIDI per part, WAV stems and a preview for FL Studio Mobile")
    exp.add_argument("path", nargs="?", help="track JSON (default: the newest composed track)")
    _add_export_args(exp)
    exp.set_defaults(func=_cmd_export)

    show = sub.add_parser("show", help="summarize a composed track JSON")
    show.add_argument("path")
    show.set_defaults(func=_cmd_show)

    dirs = sub.add_parser("dirs", help="show sample and output folders")
    dirs.set_defaults(func=_cmd_dirs)

    upd = sub.add_parser("update", help="update psygen to the newest version (git pull)")
    upd.add_argument("--check", action="store_true", help="only show whether an update is available")
    upd.set_defaults(func=_cmd_update)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        return _cmd_ui(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

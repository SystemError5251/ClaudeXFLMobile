"""Command line entry point. The full Termux UI arrives in a later step."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, ai, config
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
        print(
            "No sample folders found. Run `termux-setup-storage`, put samples in\n"
            "~/storage/shared/Samples, or pass a folder: psygen scan PATH --add",
            file=sys.stderr,
        )
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
    print(f"Termux: {'yes' if config.is_termux() else 'no'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="psygen", description=__doc__)
    parser.add_argument("--version", action="version", version=f"psygen {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

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

    dirs = sub.add_parser("dirs", help="show sample and output folders")
    dirs.set_defaults(func=_cmd_dirs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

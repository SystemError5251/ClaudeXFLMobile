"""Export a composed track into a folder FL Studio Mobile can use.

Layout of ``<export dir>/<track-slug>/``::

    <slug>.mid             full arrangement, one MIDI track per part
    <slug>-preview.wav     stereo mixdown (and .mp3 with ffmpeg, on request)
    midi/01-kick.mid ...   one MIDI file per part
    stems/01-kick.wav ...  one mono WAV per part, all starting at bar 1
    track.json             the composed track, for re-exporting later
    info.txt               tempo, key, arrangement and import tips
    .psygen-export         marker: the scanner skips this folder
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..compose.model import Role, Track
from ..samples.audio_io import numpy_available
from ..samples.library import EXPORT_MARKER, SampleLibrary
from . import midi, render
from .wavio import write_wav


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "track"


@dataclass
class ExportOptions:
    sample_rate: int = 44100
    bits: int = 16
    stems: bool = True
    preview: bool = True
    mp3: bool = False


@dataclass
class ExportResult:
    folder: Path
    full_midi: Path
    part_midi: dict[Role, Path] = field(default_factory=dict)
    stems: dict[Role, Path] = field(default_factory=dict)
    sources: dict[Role, str] = field(default_factory=dict)  # sample name or built-in
    preview: Path | None = None
    mp3: Path | None = None
    warnings: list[str] = field(default_factory=list)


def prepare_folder(root: Path, slug: str) -> Path:
    """``root/slug``, cleared of a previous export of ours. A folder with the
    same name that psygen did not create is left alone; a suffix is added."""
    folder = root / slug
    n = 2
    while folder.exists() and not (folder / EXPORT_MARKER).exists():
        folder = root / f"{slug}-{n}"
        n += 1
    if folder.exists():
        for sub, pattern in (("midi", "*.mid"), ("stems", "*.wav")):
            for old in (folder / sub).glob(pattern):
                old.unlink()
        for old in folder.glob("*-preview.*"):
            old.unlink()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / EXPORT_MARKER).write_text("Exported by psygen; not scanned as samples.\n")
    return folder


def _info_text(track: Track, result: ExportResult) -> str:
    minutes, seconds = divmod(round(track.duration_seconds), 60)
    lines = [
        track.title,
        f"{track.genre}, {track.bpm:g} BPM, {track.key}, {track.total_bars} bars ({minutes}:{seconds:02d})",
    ]
    if track.notes:
        lines.append(track.notes)
    lines += ["", "Arrangement:"]
    for s in track.sections:
        roles = ", ".join(r.value for r in track.section_patterns(s))
        lines.append(f"  bar {s.start_bar + 1:>3}  {s.kind.value:<9} {s.bars:>3} bars  {s.name}: {roles}")
    lines += ["", "Parts:"]
    for role in track.roles_used():
        source = result.sources.get(role, "")
        lines.append(f"  {midi.part_file_name(role):<12} {source}")
    lines += [
        "",
        "In FL Studio Mobile:",
        f"  - Set the project tempo to {track.bpm:g} BPM.",
        "  - Audio: add each WAV from stems/ as an audio clip at bar 1 (or load the",
        "    whole preview). All stems have the same length, so they stay in sync.",
        "  - MIDI: load the part's sample (or any instrument) into a channel and",
        "    import midi/NN-part.mid into its piano roll. Drum and one-shot parts",
        "    play on C5, so a sampler plays them at their own pitch.",
        f"  - {result.full_midi.name} holds every part at once (General MIDI drums on",
        "    channel 10) for other apps or a quick listen.",
    ]
    return "\n".join(lines) + "\n"


def _mp3(wav: Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    out = wav.with_suffix(".mp3")
    cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-i", str(wav), "-codec:a", "libmp3lame", "-q:a", "2", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if proc.returncode == 0 and out.exists() else None


def export_track(
    track: Track,
    root: str | Path,
    options: ExportOptions | None = None,
    library: SampleLibrary | None = None,
    progress: Callable[[str], None] | None = None,
) -> ExportResult:
    """Write MIDI, stems and preview for ``track`` into ``root/<slug>/``."""
    options = options or ExportOptions()
    say = progress or (lambda _msg: None)
    slug = slugify(track.title)
    folder = prepare_folder(Path(root).expanduser(), slug)
    track.save(folder / "track.json")

    full, parts = midi.write_midi(track, folder, slug)
    result = ExportResult(folder, full, parts)
    say(f"MIDI: {len(parts)} parts")

    want_audio = options.stems or options.preview
    if want_audio and not numpy_available():
        result.warnings.append("numpy is not installed, so only MIDI was written (pkg install python-numpy)")
        want_audio = False

    if want_audio:
        sr = options.sample_rate
        n_total = render.total_samples(track, sr)
        mix = render.new_mix(n_total) if options.preview else None
        for role in track.roles_used():
            voice = render.make_voice(track, role, sr, library, result.warnings)
            result.sources[role] = voice.describe()
            say(f"rendering {role.value} ({result.sources[role]})")
            stem = render.render_stem(track, role, voice, n_total)
            if not stem.any():
                continue
            render.apply_mix_gain(stem, role, result.warnings)
            if options.stems:
                path = folder / "stems" / f"{midi.part_file_name(role)}.wav"
                result.stems[role] = write_wav(path, stem, sr, options.bits)
            if mix is not None:
                render.pan_into(mix, stem, role)
            del stem
        if mix is not None:
            say("mixing preview")
            render.master(mix)
            result.preview = write_wav(folder / f"{slug}-preview.wav", mix.T, sr, options.bits)
            del mix
            if options.mp3:
                result.mp3 = _mp3(result.preview)
                if result.mp3 is None:
                    result.warnings.append("MP3 not written: ffmpeg missing or failed (pkg install ffmpeg)")
    else:
        for role in track.roles_used():
            part = track.parts.get(role)
            result.sources[role] = Path(part.sample).name if part and part.sample else "built-in sound"

    (folder / "info.txt").write_text(_info_text(track, result), encoding="utf-8")
    return result

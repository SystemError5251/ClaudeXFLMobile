"""Let Claude compose a track, then pull it into shape with the genre rules.

One request asks Claude for the whole piece: tempo, key, which of the user's
samples to use, the note patterns per part and the arrangement. Notes come
back as scale degrees, so Claude never has to think about MIDI registers and
melodic parts start out in key. :func:`fix_track` then repairs whatever
still breaks the genre rules.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from .. import ai
from ..samples import SampleLibrary
from ..samples.library import Sample
from .genres import GenreProfile, get_genre
from .model import PATTERN_BARS, Note, Part, Pattern, Role, Section, SectionKind, Track
from .rules import fix_track
from .theory import NOTE_NAMES, SCALES, degree_to_pitch, key_name, parse_key

SAMPLES_PER_ROLE = 8
COMPOSE_MAX_TOKENS = 64000


@dataclass
class ComposeRequest:
    genre: str = "psytrance"
    idea: str = ""  # free text from the user: mood, references, anything
    bpm: float | None = None
    key: str | None = None
    scale: str | None = None
    minutes: float | None = None
    seed: int | None = None

    def profile(self) -> GenreProfile:
        return get_genre(self.genre)

    def target_bars(self) -> int | None:
        if not self.minutes:
            return None
        genre = self.profile()
        bpm = self.bpm or genre.default_bpm
        bars = self.minutes * bpm / 4
        return max(genre.phrase_bars * 8, round(bars / genre.phrase_bars) * genre.phrase_bars)


@dataclass
class ComposeResult:
    track: Track
    fixes: list[str] = field(default_factory=list)
    raw: Any = None  # Claude's answer before repairs, for debugging


# -- samples -----------------------------------------------------------------


def shortlist_samples(
    library: SampleLibrary | None, rng: random.Random, per_role: int = SAMPLES_PER_ROLE
) -> dict[int, tuple[Role, Sample]]:
    """A few candidate samples per role, numbered for the prompt.

    Drums and melodic parts get one-shots (the notes come from the patterns);
    atmos, FX and vocals may be loops since they are triggered once.
    """
    out: dict[int, tuple[Role, Sample]] = {}
    if library is None or not len(library):
        return out
    next_id = 1
    for role in Role:
        loops = None if role in (Role.ATMOS, Role.FX, Role.VOCAL) else False
        pool: list[Sample] = []
        seen: set[str] = set()
        for category in role.info.categories:
            for sample in library.by_category(category, loops):
                if sample.path not in seen:
                    seen.add(sample.path)
                    pool.append(sample)
        if not pool:
            continue
        # Confident matches first, shuffled within the same confidence so
        # every run sees a slightly different selection.
        rng.shuffle(pool)
        pool.sort(key=lambda s: -s.confidence)
        for sample in pool[:per_role]:
            out[next_id] = (role, sample)
            next_id += 1
    return out


def _sample_line(i: int, sample: Sample) -> dict:
    entry: dict[str, Any] = {"id": i, "name": sample.name}
    if sample.is_loop:
        entry["loop"] = True
    if sample.bpm:
        entry["bpm"] = sample.bpm
    if sample.key:
        entry["key"] = sample.key
    if sample.duration:
        entry["sec"] = round(sample.duration, 2)
    return entry


def fallback_sample(library: SampleLibrary | None, role: Role, rng: random.Random) -> str | None:
    if library is None:
        return None
    loops = None if role in (Role.ATMOS, Role.FX, Role.VOCAL) else False
    for category in role.info.categories:
        sample = library.pick(category, rng=rng, loops=loops)
        if sample is not None:
            return sample.path
    return None


# -- prompt ------------------------------------------------------------------

SYSTEM = """You are a producer of psytrance, goa trance and progressive psytrance who writes complete tracks as data. A Python program turns your answer into MIDI and audio stems for FL Studio Mobile, using the producer's own samples.

Write music that sounds finished and intentional: a clear idea, parts that interlock, variation every 8 bars and real development across the arrangement. Prefer a few strong patterns used well over many weak ones.

Format rules:
- Time is 4/4 on a 16th-note grid: 16 steps per bar, step 0 is the first 16th of the pattern.
- A pattern is a loop of 1, 2, 4 or 8 bars for one part. It repeats until its section ends.
- A note is [step, degree, length, velocity]. degree is a scale degree of the track's scale: 0 is the root, 1-6 the other notes, 7 the root an octave up, negative values go below. The program places every part in its own register, so think in degrees, not octaves. For drum, atmos, FX and vocal parts degree is ignored; use 0. length is in 16ths, velocity is 1-127.
- A section names the patterns it plays, at most one per part. Section lengths are multiples of 8 bars.
- Section kinds: intro, build, drop, breakdown, outro. Start with an intro, end with an outro, have at least one drop.

Genre rules the program enforces (follow them so nothing has to be changed):
- The kick is four-on-the-floor on steps 0, 4, 8, 12 of every bar. Every drop has kick and bass. Breakdowns have no kick and no bass.
- Bass notes never start on a kick step (step % 4 == 0) and end before the next kick. The bass is monophonic and mostly sits on the root, with movement for tension.
- Open hats sit on the offbeat (step % 4 == 2).
- Bass, arp and lead are monophonic.

To vary a part across sections, write several patterns for it (for example bass_a and bass_b) and use different ones per section. Builds should add tension (snare or clap rolls in the last bars of a longer pattern, rising FX, filtered or sparser bass). Use sample ids from the list for each part; use -1 when no listed sample fits and the part should be played by a synth."""


def _schema() -> dict:
    roles = [r.value for r in Role]
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "idea": {"type": "string", "description": "One or two sentences on the track's concept."},
            "bpm": {"type": "number"},
            "key": {"type": "string", "enum": list(NOTE_NAMES)},
            "scale": {"type": "string", "enum": list(SCALES)},
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "enum": roles},
                        "sample": {"type": "integer"},
                    },
                    "required": ["role", "sample"],
                    "additionalProperties": False,
                },
            },
            "patterns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "role": {"type": "string", "enum": roles},
                        "bars": {"type": "integer", "enum": list(PATTERN_BARS)},
                        "notes": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
                    },
                    "required": ["id", "role", "bars", "notes"],
                    "additionalProperties": False,
                },
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": [k.value for k in SectionKind]},
                        "bars": {"type": "integer"},
                        "energy": {"type": "integer", "description": "1-10"},
                        "patterns": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "kind", "bars", "energy", "patterns"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "idea", "bpm", "key", "scale", "parts", "patterns", "sections"],
        "additionalProperties": False,
    }


def build_prompt(request: ComposeRequest, shortlist: dict[int, tuple[Role, Sample]]) -> str:
    genre = request.profile()
    lines = [f"Compose a {genre.name} track.", "", f"Genre: {genre.style}"]
    lo, hi = genre.bpm_range
    if request.bpm:
        lines.append(f"Tempo: {request.bpm:g} BPM.")
    else:
        lines.append(f"Tempo: pick between {lo} and {hi} BPM (typical {genre.default_bpm}).")
    root = parse_key(request.key)
    if root is not None:
        lines.append(f"Key: {key_name(root)}.")
    if request.scale:
        lines.append(f"Scale: {request.scale}.")
    else:
        lines.append(f"Scale: one of {', '.join(genre.scales)}.")
    target = request.target_bars()
    if target:
        lines.append(f"Length: about {target} bars in total.")
    else:
        lines.append(f"Length: {genre.bar_range[0]}-{genre.bar_range[1]} bars in total.")
    example = " -> ".join(f"{k.value} {b}" for k, b, _ in genre.template)
    lines.append(f"A typical arrangement in bars: {example}. Shape your own.")
    if request.idea.strip():
        lines += ["", "The producer's idea for this track:", request.idea.strip()]

    lines += ["", "Parts you can use: " + ", ".join(r.value for r in Role) + "."]
    if shortlist:
        by_role: dict[Role, list[dict]] = {}
        for i, (role, sample) in shortlist.items():
            by_role.setdefault(role, []).append(_sample_line(i, sample))
        lines += ["", "The producer's samples per part (choose by id):"]
        for role, entries in by_role.items():
            lines.append(f"{role.value}: " + json.dumps(entries, separators=(",", ":")))
        missing = [r.value for r in Role if r not in by_role]
        if missing:
            lines.append("No samples for: " + ", ".join(missing) + " (use -1, a synth plays them).")
    else:
        lines += ["", "The producer has no samples indexed yet; use -1 for every part."]
    return "\n".join(lines)


# -- parsing -----------------------------------------------------------------


def parse_answer(
    answer: dict,
    request: ComposeRequest,
    shortlist: dict[int, tuple[Role, Sample]],
) -> Track:
    """Turn Claude's JSON into a :class:`Track`. Unknown values are skipped
    here and the rule pass deals with what is left."""
    genre = request.profile()
    root = parse_key(request.key)
    if root is None:
        root = parse_key(answer.get("key"))
    scale = request.scale or answer.get("scale")
    if scale not in SCALES:
        scale = genre.default_scale
    try:
        bpm = float(request.bpm or answer.get("bpm") or genre.default_bpm)
    except (TypeError, ValueError):
        bpm = float(genre.default_bpm)
    track = Track(
        title=str(answer.get("title") or f"Untitled {genre.name}").strip()[:80],
        genre=genre.name,
        bpm=bpm,
        root=root if root is not None else 9,
        scale=scale,
        notes=str(answer.get("idea") or ""),
    )

    for entry in answer.get("parts", []):
        try:
            role = Role(entry.get("role"))
        except ValueError:
            continue
        pick = shortlist.get(entry.get("sample"))
        # Only accept a sample that was offered for this part.
        sample = pick[1].path if pick and pick[0] is role else None
        track.parts[role] = Part(role, sample)

    for entry in answer.get("patterns", []):
        try:
            role = Role(entry.get("role"))
        except ValueError:
            continue
        pid = str(entry.get("id") or "").strip()
        if not pid or pid in track.patterns:
            continue
        notes = []
        for raw in entry.get("notes", []):
            if not isinstance(raw, list) or len(raw) < 2 or not all(isinstance(v, int) for v in raw):
                continue
            step, degree = raw[0], raw[1]
            length = raw[2] if len(raw) > 2 else 1
            velocity = raw[3] if len(raw) > 3 else 100
            if role.is_drum:
                pitch = role.info.note
            else:
                pitch = degree_to_pitch(degree, track.root, track.scale, role.info.base_octave)
            notes.append(Note(step, pitch, length, velocity))
        bars = entry.get("bars") if isinstance(entry.get("bars"), int) else 1
        track.patterns[pid] = Pattern(pid, role, bars, notes)

    for entry in answer.get("sections", []):
        try:
            kind = SectionKind(entry.get("kind"))
        except ValueError:
            continue
        bars = entry.get("bars")
        energy = entry.get("energy")
        track.sections.append(
            Section(
                name=str(entry.get("name") or kind.value).strip()[:40],
                kind=kind,
                bars=bars if isinstance(bars, int) and bars > 0 else 16,
                patterns=[str(p) for p in entry.get("patterns", [])],
                energy=energy if isinstance(energy, int) else 5,
            )
        )
    track.layout()
    return track


def _fill_samples(track: Track, library: SampleLibrary | None, rng: random.Random) -> None:
    for part in track.parts.values():
        if part.sample is None:
            part.sample = fallback_sample(library, part.role, rng)


# -- entry points ------------------------------------------------------------


def compose(
    request: ComposeRequest,
    library: SampleLibrary | None = None,
    *,
    client=None,
    model: str = ai.DEFAULT_MODEL,
    effort: str = "high",
) -> ComposeResult:
    """Ask Claude for a track and repair it. Raises :class:`ai.ClaudeError`."""
    genre = request.profile()  # raises early on an unknown genre
    rng = random.Random(request.seed)
    shortlist = shortlist_samples(library, rng)
    answer = ai.ask_json(
        SYSTEM,
        build_prompt(request, shortlist),
        _schema(),
        client=client,
        model=model,
        effort=effort,
        max_tokens=COMPOSE_MAX_TOKENS,
    )
    if not isinstance(answer, dict):
        raise ai.ClaudeError("Claude's answer was not a track")
    track = parse_answer(answer, request, shortlist)
    fixes = fix_track(track, genre, request.target_bars())
    _fill_samples(track, library, rng)
    return ComposeResult(track, fixes, answer)


def skeleton(request: ComposeRequest, library: SampleLibrary | None = None) -> ComposeResult:
    """A plain track from the genre template, without Claude. Useful offline
    and as a known-good input for the exporters."""
    genre = request.profile()
    rng = random.Random(request.seed)
    root = parse_key(request.key)
    if root is None:
        root = rng.randrange(12)
    scale = request.scale if request.scale in SCALES else genre.default_scale
    track = Track(
        title=f"{genre.name.capitalize()} sketch in {key_name(root)}",
        genre=genre.name,
        bpm=float(request.bpm or genre.default_bpm),
        root=root,
        scale=scale,
        notes="Template arrangement with default patterns (offline).",
    )
    fixes = fix_track(track, genre, request.target_bars())
    # The template and its default patterns are the point here, not repairs.
    fixes = [f for f in fixes if not f.startswith(("added a default", "no arrangement"))]
    _fill_samples(track, library, rng)
    return ComposeResult(track, fixes)


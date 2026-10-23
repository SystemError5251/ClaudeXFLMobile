"""In-memory track model shared by the composer and the exporters.

Time is counted in 16th-note steps: 16 steps per 4/4 bar. A :class:`Pattern`
is a loop of 1, 2, 4 or 8 bars for one part. A :class:`Section` lays patterns
over a run of bars and repeats each one until the section ends. Pitches are
MIDI note numbers; drum parts use their General MIDI drum note so the MIDI
export plays something sensible even without a sample.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator

from ..samples.categorize import Category
from .theory import key_name

STEPS_PER_BAR = 16
STEPS_PER_BEAT = 4
PATTERN_BARS = (1, 2, 4, 8)
MODEL_VERSION = 1


class SectionKind(str, Enum):
    INTRO = "intro"
    BUILD = "build"
    DROP = "drop"
    BREAKDOWN = "breakdown"
    OUTRO = "outro"


@dataclass(frozen=True)
class RoleInfo:
    drum: bool
    categories: tuple[Category, ...]  # sample categories to choose from, best first
    note: int = 60  # GM drum note for drum roles
    low: int = 0  # playable range for melodic roles
    high: int = 127
    base_octave: int = 4  # octave of scale degree 0
    mono: bool = False
    midi_channel: int = 0  # 0-based; 9 is the GM drum channel


class Role(str, Enum):
    KICK = "kick"
    BASS = "bass"
    HAT = "hat"
    OPEN_HAT = "open_hat"
    CLAP = "clap"
    SNARE = "snare"
    PERC = "perc"
    RIDE = "ride"
    ARP = "arp"
    LEAD = "lead"
    PAD = "pad"
    ATMOS = "atmos"
    FX = "fx"
    VOCAL = "vocal"

    @property
    def info(self) -> RoleInfo:
        return ROLE_INFO[self]

    @property
    def is_drum(self) -> bool:
        return ROLE_INFO[self].drum


C = Category
ROLE_INFO: dict[Role, RoleInfo] = {
    Role.KICK: RoleInfo(True, (C.KICK,), note=36, midi_channel=9),
    Role.BASS: RoleInfo(False, (C.BASS, C.SYNTH), low=28, high=52, base_octave=1, mono=True, midi_channel=0),
    Role.HAT: RoleInfo(True, (C.HAT_CLOSED,), note=42, midi_channel=9),
    Role.OPEN_HAT: RoleInfo(True, (C.HAT_OPEN,), note=46, midi_channel=9),
    Role.CLAP: RoleInfo(True, (C.CLAP, C.SNARE), note=39, midi_channel=9),
    Role.SNARE: RoleInfo(True, (C.SNARE, C.CLAP), note=38, midi_channel=9),
    Role.PERC: RoleInfo(True, (C.PERC,), note=50, midi_channel=9),
    Role.RIDE: RoleInfo(True, (C.CYMBAL,), note=51, midi_channel=9),
    Role.ARP: RoleInfo(False, (C.ARP, C.PLUCK, C.SYNTH), low=55, high=84, base_octave=4, mono=True, midi_channel=1),
    Role.LEAD: RoleInfo(False, (C.LEAD, C.SYNTH, C.PLUCK), low=60, high=91, base_octave=5, mono=True, midi_channel=2),
    Role.PAD: RoleInfo(False, (C.PAD, C.SYNTH), low=48, high=79, base_octave=4, midi_channel=3),
    Role.ATMOS: RoleInfo(True, (C.ATMOS, C.PAD), note=60, midi_channel=4),
    Role.FX: RoleInfo(True, (C.FX,), note=60, midi_channel=5),
    Role.VOCAL: RoleInfo(True, (C.VOCAL,), note=60, midi_channel=6),
}
del C


@dataclass
class Note:
    step: int  # start within the pattern, in 16ths
    pitch: int
    length: int = 1  # in 16ths
    velocity: int = 100


@dataclass
class Pattern:
    id: str
    role: Role
    bars: int
    notes: list[Note] = field(default_factory=list)

    @property
    def steps(self) -> int:
        return self.bars * STEPS_PER_BAR


@dataclass
class Part:
    role: Role
    sample: str | None = None  # path of the chosen sample, None = synth/MIDI only

    @property
    def midi_channel(self) -> int:
        return self.role.info.midi_channel


@dataclass
class Section:
    name: str
    kind: SectionKind
    bars: int
    patterns: list[str] = field(default_factory=list)  # pattern ids, at most one per role
    energy: int = 5  # 1-10, a hint for mixing and FX in later steps
    start_bar: int = 0  # filled in by Track.layout()

    @property
    def end_bar(self) -> int:
        return self.start_bar + self.bars


@dataclass
class AbsoluteNote:
    """A note placed on the song timeline (steps from the start of the track)."""

    role: Role
    step: int
    pitch: int
    length: int
    velocity: int
    section: str


@dataclass
class Track:
    title: str
    genre: str
    bpm: float
    root: int  # pitch class 0-11
    scale: str
    parts: dict[Role, Part] = field(default_factory=dict)
    patterns: dict[str, Pattern] = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    notes: str = ""  # Claude's short description of the idea

    # -- structure ----------------------------------------------------------

    def layout(self) -> None:
        """Recompute section start bars so sections follow each other."""
        bar = 0
        for s in self.sections:
            s.start_bar = bar
            bar += s.bars

    @property
    def total_bars(self) -> int:
        return sum(s.bars for s in self.sections)

    @property
    def total_steps(self) -> int:
        return self.total_bars * STEPS_PER_BAR

    @property
    def duration_seconds(self) -> float:
        return self.total_bars * 4 * 60.0 / self.bpm

    @property
    def key(self) -> str:
        return f"{key_name(self.root)} {self.scale.replace('_', ' ')}"

    def section_patterns(self, section: Section) -> dict[Role, Pattern]:
        out: dict[Role, Pattern] = {}
        for pid in section.patterns:
            p = self.patterns.get(pid)
            if p is not None and p.role not in out:
                out[p.role] = p
        return out

    def roles_used(self) -> list[Role]:
        used = {r for s in self.sections for r in self.section_patterns(s)}
        return [r for r in Role if r in used]

    def timeline(self, role: Role | None = None) -> Iterator[AbsoluteNote]:
        """All notes in song order, patterns repeated across their sections.
        Notes that would run past a section's end are shortened."""
        for s in self.sections:
            start = s.start_bar * STEPS_PER_BAR
            end = s.end_bar * STEPS_PER_BAR
            for r, p in self.section_patterns(s).items():
                if role is not None and r is not role:
                    continue
                for offset in range(start, end, p.steps):
                    for n in p.notes:
                        at = offset + n.step
                        if at >= end:
                            continue
                        length = min(n.length, end - at)
                        yield AbsoluteNote(r, at, n.pitch, length, n.velocity, s.name)

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": MODEL_VERSION,
            "title": self.title,
            "genre": self.genre,
            "bpm": self.bpm,
            "root": self.root,
            "scale": self.scale,
            "notes": self.notes,
            "parts": [{"role": p.role.value, "sample": p.sample} for p in self.parts.values()],
            "patterns": [
                {
                    "id": p.id,
                    "role": p.role.value,
                    "bars": p.bars,
                    "notes": [[n.step, n.pitch, n.length, n.velocity] for n in p.notes],
                }
                for p in self.patterns.values()
            ],
            "sections": [
                {k: (v.value if isinstance(v, Enum) else v) for k, v in asdict(s).items()}
                for s in self.sections
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        if d.get("version") != MODEL_VERSION:
            raise ValueError(f"unsupported track version {d.get('version')!r}")
        track = cls(
            title=d["title"],
            genre=d["genre"],
            bpm=float(d["bpm"]),
            root=int(d["root"]),
            scale=d["scale"],
            notes=d.get("notes", ""),
        )
        for p in d.get("parts", []):
            role = Role(p["role"])
            track.parts[role] = Part(role, p.get("sample"))
        for p in d.get("patterns", []):
            notes = [Note(*n) for n in p.get("notes", [])]
            track.patterns[p["id"]] = Pattern(p["id"], Role(p["role"]), int(p["bars"]), notes)
        for s in d.get("sections", []):
            track.sections.append(
                Section(
                    name=s["name"],
                    kind=SectionKind(s["kind"]),
                    bars=int(s["bars"]),
                    patterns=list(s.get("patterns", [])),
                    energy=int(s.get("energy", 5)),
                    start_bar=int(s.get("start_bar", 0)),
                )
            )
        track.layout()
        return track

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Track":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

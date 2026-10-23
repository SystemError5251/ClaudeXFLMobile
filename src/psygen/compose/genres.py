"""Genre profiles: tempo, scales, structure and fallback patterns.

The profiles feed both the prompt (so Claude knows the conventions) and the
rule checker (so a broken answer gets pulled back into the genre). The
default patterns are deliberately plain; they fill gaps when Claude leaves an
essential part out, and make up the offline skeleton track.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Note, Pattern, Role, SectionKind
from .theory import degree_to_pitch


@dataclass(frozen=True)
class GenreProfile:
    name: str
    bpm_range: tuple[int, int]
    default_bpm: int
    scales: tuple[str, ...]  # preferred first
    bar_range: tuple[int, int]  # sensible total length in bars
    phrase_bars: int  # sections are whole multiples of this
    # 16th positions within a beat where bass notes may start. Position 0 is
    # the kick, so it is never allowed.
    bass_offsets: tuple[int, ...]
    template: tuple[tuple[SectionKind, int, tuple[Role, ...]], ...]
    style: str  # prompt text: what makes the genre

    @property
    def default_scale(self) -> str:
        return self.scales[0]


K, B, H, OH, CL, PC, RD, AR, LD, PD, AT, FX = (
    Role.KICK, Role.BASS, Role.HAT, Role.OPEN_HAT, Role.CLAP, Role.PERC, Role.RIDE,
    Role.ARP, Role.LEAD, Role.PAD, Role.ATMOS, Role.FX,
)
I, BU, D, BD, O = (
    SectionKind.INTRO, SectionKind.BUILD, SectionKind.DROP, SectionKind.BREAKDOWN, SectionKind.OUTRO,
)

GENRES: dict[str, GenreProfile] = {
    "psytrance": GenreProfile(
        name="psytrance",
        bpm_range=(138, 150),
        default_bpm=145,
        scales=("phrygian", "harmonic_minor", "phrygian_dominant", "aeolian"),
        bar_range=(224, 320),
        phrase_bars=8,
        bass_offsets=(1, 2, 3),
        template=(
            (I, 32, (K, H, AT, FX)),
            (BU, 16, (K, B, H, CL, PC)),
            (D, 64, (K, B, H, OH, CL, PC, AR)),
            (BD, 32, (PD, LD, AT, FX)),
            (BU, 16, (K, B, H, CL, AR, FX)),
            (D, 64, (K, B, H, OH, CL, PC, AR, LD)),
            (O, 32, (K, B, H, AT)),
        ),
        style=(
            "Full-on / forest psytrance: driving four-on-the-floor kick, rolling "
            "16th bass in the three 16ths after each kick (K-B-B-B), dark phrygian "
            "or harmonic minor colour, acid-style squelchy leads, 16th arps and "
            "call-and-response FX. Changes every 8 bars, bigger changes every 16 "
            "or 32 bars."
        ),
    ),
    "goa": GenreProfile(
        name="goa",
        bpm_range=(138, 150),
        default_bpm=145,
        scales=("phrygian_dominant", "harmonic_minor", "hungarian_minor", "phrygian"),
        bar_range=(256, 352),
        phrase_bars=8,
        bass_offsets=(1, 2, 3),
        template=(
            (I, 32, (K, H, PD, AT)),
            (BU, 16, (K, B, H, OH, PC)),
            (D, 64, (K, B, H, OH, CL, AR, LD)),
            (BD, 32, (PD, LD, AT)),
            (BU, 16, (K, B, H, AR, FX)),
            (D, 96, (K, B, H, OH, CL, PC, AR, LD)),
            (O, 32, (K, B, H, PD)),
        ),
        style=(
            "90s goa trance: melodic and psychedelic, eastern scales (phrygian "
            "dominant, harmonic or hungarian minor), long evolving lead melodies "
            "that answer each other, layered arps, rolling or galloping bass that "
            "jumps octaves, open hats on the offbeat, lush pads in breakdowns."
        ),
    ),
    "progressive": GenreProfile(
        name="progressive",
        bpm_range=(130, 140),
        default_bpm=136,
        scales=("aeolian", "dorian", "phrygian"),
        bar_range=(256, 384),
        phrase_bars=8,
        bass_offsets=(1, 2, 3),
        template=(
            (I, 32, (K, H, RD, AT)),
            (BU, 32, (K, B, H, OH, PC)),
            (D, 64, (K, B, H, OH, CL, PC, AR)),
            (BD, 32, (PD, AR, AT, FX)),
            (BU, 16, (K, B, H, CL, AR, FX)),
            (D, 64, (K, B, H, OH, CL, PC, AR, LD, PD)),
            (O, 32, (K, B, H, RD)),
        ),
        style=(
            "Progressive psytrance: groovy and hypnotic, 132-138 BPM, offbeat "
            "bass (K-B-B or K-x-B-B), minimal melodic hooks that develop slowly, "
            "plucky arps with space, rides and percussion carry the groove, "
            "aeolian or dorian mood. Fewer notes, more swing and tension."
        ),
    ),
}
del K, B, H, OH, CL, PC, RD, AR, LD, PD, AT, FX, I, BU, D, BD, O


def get_genre(name: str) -> GenreProfile:
    key = name.strip().lower().replace(" ", "").replace("-", "")
    aliases = {"psy": "psytrance", "fullon": "psytrance", "goatrance": "goa",
               "prog": "progressive", "progressivepsy": "progressive",
               "progressivepsytrance": "progressive", "progressivetrance": "progressive"}
    key = aliases.get(key, key)
    if key not in GENRES:
        raise ValueError(f"unknown genre {name!r} (choose from {', '.join(GENRES)})")
    return GENRES[key]


# -- fallback patterns -------------------------------------------------------


def _drum(role: Role, steps_vel: list[tuple[int, int]], bars: int = 1) -> Pattern:
    note = role.info.note
    return Pattern(f"{role.value}_default", role, bars, [Note(s, note, 1, v) for s, v in steps_vel])


def default_pattern(role: Role, genre: GenreProfile, root: int, scale: str) -> Pattern:
    """A plain, rule-conforming pattern for ``role`` in ``genre``."""
    def pitch(degree: int) -> int:
        return degree_to_pitch(degree, root, scale, role.info.base_octave)

    name = genre.name
    if role is Role.KICK:
        return _drum(role, [(b * 4, 120) for b in range(4)])
    if role is Role.HAT:
        if name == "progressive":
            return _drum(role, [(b * 4 + 2, 90) for b in range(4)])
        return _drum(role, [(s, 95 if s % 4 == 2 else 55) for s in range(16) if s % 4])
    if role is Role.OPEN_HAT:
        return _drum(role, [(b * 4 + 2, 85) for b in range(4)])
    if role in (Role.CLAP, Role.SNARE):
        return _drum(role, [(4, 105), (12, 105)])
    if role is Role.PERC:
        return _drum(role, [(3, 70), (7, 60), (11, 75), (14, 65)])
    if role is Role.RIDE:
        return _drum(role, [(b * 4 + 2, 70) for b in range(4)])
    if role in (Role.ATMOS, Role.FX, Role.VOCAL):
        return Pattern(f"{role.value}_default", role, 8, [Note(0, role.info.note, 32, 90)])
    if role is Role.BASS:
        notes = []
        offsets = (2, 3) if name == "progressive" else (1, 2, 3)
        for bar in range(2):
            for beat in range(4):
                for off in offsets:
                    deg = 0
                    if name == "goa" and off == 3 and beat % 2:
                        deg = 7
                    if bar == 1 and beat == 3:
                        deg = 1 if scale in ("phrygian", "phrygian_dominant") else -2
                    vel = 100 if off == offsets[0] else 82
                    notes.append(Note(bar * 16 + beat * 4 + off, pitch(deg), 1, vel))
        return Pattern("bass_default", role, 2, notes)
    if role is Role.ARP:
        shape = (0, 2, 4, 7, 4, 2, 0, 2) if name != "progressive" else (0, None, 4, None, 7, None, 4, 2)
        notes = [
            Note(i, pitch(shape[i % len(shape)]), 1, 85 if i % 4 == 0 else 70)
            for i in range(16) if shape[i % len(shape)] is not None
        ]
        return Pattern("arp_default", role, 1, notes)
    if role is Role.LEAD:
        motif = [(0, 4, 4), (6, 3, 2), (8, 2, 4), (12, 1, 4), (16, 0, 8), (28, 1, 2), (30, 2, 2)]
        notes = [Note(s, pitch(d), l, 100) for s, d, l in motif]
        return Pattern("lead_default", role, 4, notes)
    if role is Role.PAD:
        notes = []
        for bar, chord in enumerate(((0, 2, 4), (0, 2, 4), (-2, 0, 3), (-1, 1, 4))):
            notes += [Note(bar * 16, pitch(d), 16, 70) for d in chord]
        return Pattern("pad_default", role, 4, notes)
    raise ValueError(role)

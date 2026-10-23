"""Keys, scales and pitch helpers."""

from __future__ import annotations

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLATS = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#", "CB": "B", "FB": "E"}

# Interval sets, root first. Psytrance and goa live mostly in phrygian and
# harmonic minor flavours; progressive leans on aeolian and dorian.
SCALES: dict[str, tuple[int, ...]] = {
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "phrygian_dominant": (0, 1, 4, 5, 7, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
    "aeolian": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "hungarian_minor": (0, 2, 3, 6, 7, 8, 11),
}


def parse_key(text: str | None) -> int | None:
    """Pitch class (0-11) of a key name like 'F#', 'Gb', 'F#m' or 'a minor'."""
    if not text:
        return None
    t = text.strip().upper().replace("♯", "#").replace("♭", "B")
    for suffix in ("MINOR", "MAJOR", "MIN", "MAJ", "M"):
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[: -len(suffix)].strip()
            break
    if len(t) >= 2 and t[:2] in _FLATS:
        t = _FLATS[t[:2]]
    t = t[:2] if len(t) >= 2 and t[1] == "#" else t[:1]
    return NOTE_NAMES.index(t) if t in NOTE_NAMES else None


def key_name(pc: int) -> str:
    return NOTE_NAMES[pc % 12]


def pitch_name(pitch: int) -> str:
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"


def scale_pitch_classes(root: int, scale: str) -> frozenset[int]:
    return frozenset((root + i) % 12 for i in SCALES[scale])


def degree_to_pitch(degree: int, root: int, scale: str, base_octave: int) -> int:
    """MIDI pitch of a scale degree. Degree 0 is the root in ``base_octave``
    (MIDI octave numbering, C4 = 60); 7 is the root an octave up, -1 the
    degree below the root."""
    steps = SCALES[scale]
    octave, idx = divmod(degree, len(steps))
    return 12 * (base_octave + 1 + octave) + root % 12 + steps[idx]


def snap_to_scale(pitch: int, root: int, scale: str) -> int:
    """Nearest pitch in the scale, preferring the lower neighbour on ties."""
    allowed = scale_pitch_classes(root, scale)
    for offset in (0, -1, 1, -2, 2):
        if (pitch + offset) % 12 in allowed:
            return pitch + offset
    return pitch  # unreachable for 7-note scales


def fold_into_range(pitch: int, lo: int, hi: int) -> int:
    """Shift by octaves until ``lo <= pitch <= hi`` (range must span an octave)."""
    while pitch < lo:
        pitch += 12
    while pitch > hi:
        pitch -= 12
    return pitch

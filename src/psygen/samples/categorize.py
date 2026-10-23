"""Name-based sample categorization.

Sample packs are usually well named, so file and folder names are the primary
signal. Audio analysis (see :mod:`psygen.samples.analysis`) only steps in when
the name says nothing useful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Category(str, Enum):
    KICK = "kick"
    SNARE = "snare"
    CLAP = "clap"
    HAT_CLOSED = "hat_closed"
    HAT_OPEN = "hat_open"
    CYMBAL = "cymbal"
    PERC = "perc"
    DRUMS = "drums"  # mixed kits and full or top drum loops
    BASS = "bass"
    LEAD = "lead"
    PLUCK = "pluck"
    ARP = "arp"
    PAD = "pad"
    SYNTH = "synth"
    ATMOS = "atmos"
    FX = "fx"
    VOCAL = "vocal"
    UNKNOWN = "unknown"

    @property
    def is_drum(self) -> bool:
        return self in DRUM_CATEGORIES


DRUM_CATEGORIES = frozenset(
    {
        Category.KICK,
        Category.SNARE,
        Category.CLAP,
        Category.HAT_CLOSED,
        Category.HAT_OPEN,
        Category.CYMBAL,
        Category.PERC,
        Category.DRUMS,
    }
)

# Whole-token keywords per category. Order matters only for ties: earlier
# categories win, so specific drums beat generic "perc" and "synth".
KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.KICK: ("kick", "kicks", "kik", "kck", "bd", "bassdrum", "kickdrum"),
    Category.HAT_OPEN: ("oh", "ohh", "ohat", "openhat", "openhihat"),
    Category.HAT_CLOSED: (
        "hat", "hats", "hh", "hihat", "hihats", "chh", "ch", "chat",
        "closedhat", "closedhihat",
    ),
    Category.CLAP: ("clap", "claps", "clp", "snap", "snaps"),
    Category.SNARE: ("snare", "snares", "snr", "sd", "rimshot"),
    Category.CYMBAL: ("ride", "rides", "crash", "crashes", "cymbal", "cymbals", "splash", "china"),
    Category.BASS: (
        "bass", "basses", "bassline", "basslines", "sub", "subbass", "reese",
        "rolling", "kickbass",
    ),
    Category.ARP: ("arp", "arps", "arpeggio", "arpeggiated", "sequence", "seq"),
    Category.PLUCK: ("pluck", "plucks", "plk"),
    Category.LEAD: ("lead", "leads", "ld", "acid", "303", "riff", "hook", "melody"),
    Category.PAD: ("pad", "pads", "strings", "choir"),
    Category.ATMOS: (
        "atmos", "atmo", "atmosphere", "ambience", "ambient", "texture",
        "textures", "drone", "drones", "soundscape", "background",
    ),
    Category.FX: (
        "fx", "sfx", "riser", "risers", "uplifter", "downlifter", "downer",
        "sweep", "sweeps", "impact", "impacts", "noise", "whoosh", "zap",
        "laser", "lasers", "reverse", "rev", "transition", "build", "buildup",
        "falldown", "boom", "squelch", "glitch",
    ),
    Category.VOCAL: (
        "vox", "vocal", "vocals", "voice", "voices", "speech", "spoken",
        "talk", "chant", "shout",
    ),
    Category.PERC: (
        "perc", "percs", "percussion", "shaker", "shakers", "tom", "toms",
        "rim", "rims", "conga", "bongo", "tamb", "tambourine", "cowbell",
        "clave", "tabla", "djembe", "wood", "block", "tick", "click",
    ),
    Category.DRUMS: ("drum", "drums", "kit", "break", "breakbeat", "breaks", "top", "tops", "toploop", "fill", "fills"),
    Category.SYNTH: ("synth", "synths", "stab", "stabs", "chord", "chords", "keys", "fm", "saw"),
}

# Hats with an "open" token anywhere become open hats.
_OPEN_TOKENS = frozenset({"open", "opened", "op"})

# Substrings matched inside run-together names such as "909kickhard" or
# "psybassline". Only long, unambiguous words belong here.
SUBSTRINGS: tuple[tuple[str, Category], ...] = (
    ("kickbass", Category.BASS),
    ("openhat", Category.HAT_OPEN),
    ("hihat", Category.HAT_CLOSED),
    ("kick", Category.KICK),
    ("snare", Category.SNARE),
    ("clap", Category.CLAP),
    ("crash", Category.CYMBAL),
    ("cymbal", Category.CYMBAL),
    ("bassline", Category.BASS),
    ("riser", Category.FX),
    ("sweep", Category.FX),
    ("impact", Category.FX),
    ("atmos", Category.ATMOS),
    ("vocal", Category.VOCAL),
    ("shaker", Category.PERC),
    ("pluck", Category.PLUCK),
)

LOOP_TOKENS = frozenset({"loop", "loops", "lp", "groove", "grooves", "beat", "beats"})
ONESHOT_TOKENS = frozenset({"oneshot", "oneshots", "shot", "shots", "hit", "hits", "os"})

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SPLIT = re.compile(r"[^0-9A-Za-z#]+")
_BPM = re.compile(r"(\d{2,3})\s*-?\s*bpm", re.IGNORECASE)
_KEY = re.compile(
    r"^([a-g])(#|sharp|s|b|flat)?(m|min|minor|maj|major)?$", re.IGNORECASE
)
_NOTE_NAMES = ("c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b")
_FLATS = {"db": "c#", "eb": "d#", "gb": "f#", "ab": "g#", "bb": "a#"}


def tokenize(text: str) -> list[str]:
    """Split a file or folder name into lowercase tokens.

    ``"PsyKick_03-Hard"`` -> ``["psy", "kick", "03", "hard"]``.
    """
    tokens: list[str] = []
    for part in _SPLIT.split(text):
        if not part:
            continue
        for piece in _CAMEL.split(part):
            if piece:
                tokens.append(piece.lower())
    return tokens


def parse_bpm(text: str) -> float | None:
    m = _BPM.search(text)
    if m:
        bpm = float(m.group(1))
        return bpm if 60 <= bpm <= 220 else None
    return None


def _key_from_token(token: str, explicit: bool) -> str | None:
    m = _KEY.match(token)
    if not m:
        return None
    root, accidental, mode = m.group(1).lower(), (m.group(2) or "").lower(), (m.group(3) or "").lower()
    # A bare letter (or "fm", "bb") is too ambiguous unless it follows "key".
    if not explicit and not mode:
        return None
    if not explicit and token.lower() == "fm":
        return None
    if accidental in ("#", "sharp", "s"):
        note = root + "#"
    elif accidental in ("b", "flat"):
        note = _FLATS.get(root + "b", root)
    else:
        note = root
    if note not in _NOTE_NAMES:
        return None
    minor = mode in ("m", "min", "minor")
    return note.upper() + ("m" if minor else "")


def parse_key(text: str) -> str | None:
    """Find a musical key such as ``"F#m"`` or ``"A"`` in a name."""
    raw = [t for t in _SPLIT.split(text) if t]
    for i, token in enumerate(raw):
        explicit = i > 0 and raw[i - 1].lower() == "key"
        key = _key_from_token(token, explicit)
        if key:
            return key
    return None


@dataclass
class NameGuess:
    category: Category = Category.UNKNOWN
    confidence: float = 0.0
    is_loop: bool | None = None
    bpm: float | None = None
    key: str | None = None
    tags: list[str] = field(default_factory=list)


def _match_tokens(tokens: list[str]) -> dict[Category, float]:
    scores: dict[Category, float] = {}
    token_set = set(tokens)
    for category, words in KEYWORDS.items():
        if token_set.intersection(words):
            scores[category] = scores.get(category, 0.0) + 1.0
    if not scores:
        compact = "".join(tokens)
        for needle, category in SUBSTRINGS:
            if needle in compact:
                scores[category] = scores.get(category, 0.0) + 0.6
                break
    if Category.HAT_CLOSED in scores and token_set & _OPEN_TOKENS:
        scores[Category.HAT_OPEN] = scores.pop(Category.HAT_CLOSED)
    # "kick bass" style names describe a bass loop with the kick baked in.
    if Category.KICK in scores and Category.BASS in scores:
        scores.pop(Category.KICK)
    return scores


def categorize_name(path: str | Path, root: str | Path | None = None) -> NameGuess:
    """Guess a sample's category from its filename and parent folders.

    The filename counts most; each folder further up counts less. Folders at
    or above ``root`` (the scanned sample directory) are ignored.
    """
    path = Path(path)
    parts: list[str] = [path.stem]
    parent = path.parent
    root_path = Path(root) if root is not None else None
    for _ in range(4):
        if root_path is not None and (parent == root_path or parent == parent.parent):
            break
        if parent == parent.parent:
            break
        parts.append(parent.name)
        parent = parent.parent

    weights = [3.0, 2.0, 1.5, 1.0, 0.75]
    totals: dict[Category, float] = {}
    all_tokens: list[str] = []
    for part, weight in zip(parts, weights):
        tokens = tokenize(part)
        all_tokens.extend(tokens)
        for category, score in _match_tokens(tokens).items():
            totals[category] = totals.get(category, 0.0) + score * weight

    guess = NameGuess()
    full_text = " ".join(parts)
    guess.bpm = parse_bpm(full_text)
    guess.key = parse_key(path.stem) or next(
        (k for k in (parse_key(p) for p in parts[1:]) if k), None
    )

    token_set = set(all_tokens)
    if token_set & LOOP_TOKENS or guess.bpm is not None:
        guess.is_loop = True
    elif token_set & ONESHOT_TOKENS:
        guess.is_loop = False

    if totals:
        order = list(KEYWORDS)
        best = max(totals, key=lambda c: (totals[c], -order.index(c)))
        total = sum(totals.values())
        guess.category = best
        # A filename keyword with no rival category scores 1.0; folder-only or
        # contested matches score lower.
        guess.confidence = round(min(1.0, totals[best] / 3.0) * totals[best] / total, 2)
        guess.tags = sorted(c.value for c in totals if c is not best)
    return guess

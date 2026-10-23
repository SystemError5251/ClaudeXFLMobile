"""Genre engines and arrangement: Claude composes, the rules keep it in genre."""

from .composer import ComposeRequest, ComposeResult, compose, skeleton
from .genres import GENRES, GenreProfile, get_genre
from .model import Note, Part, Pattern, Role, Section, SectionKind, Track
from .rules import fix_track

__all__ = [
    "GENRES", "ComposeRequest", "ComposeResult", "GenreProfile", "Note", "Part", "Pattern",
    "Role", "Section", "SectionKind", "Track", "compose", "fix_track", "get_genre", "skeleton",
]

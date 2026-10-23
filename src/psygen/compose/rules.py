"""Check a composed track against genre rules and repair what breaks them.

Claude writes the music; these rules keep it inside the genre and make sure
step 3 always gets something it can export. Every repair is reported as a
short sentence so the UI can show what was changed.
"""

from __future__ import annotations

from .genres import GenreProfile, default_pattern
from .model import (
    PATTERN_BARS, STEPS_PER_BAR, STEPS_PER_BEAT, Note, Part, Pattern, Role, Section,
    SectionKind, Track,
)
from .theory import SCALES, fold_into_range, snap_to_scale

MIN_DROP_BARS = 32
DEFAULT_ENERGY = {
    SectionKind.INTRO: 3, SectionKind.BUILD: 6, SectionKind.DROP: 9,
    SectionKind.BREAKDOWN: 4, SectionKind.OUTRO: 3,
}


def fix_track(track: Track, genre: GenreProfile, target_bars: int | None = None) -> list[str]:
    """Repair ``track`` in place. Returns the list of changes made."""
    fixes: list[str] = []
    _fix_globals(track, genre, fixes)
    for pattern in list(track.patterns.values()):
        _fix_pattern(pattern, track, genre, fixes)
    _fix_sections(track, genre, fixes)
    _fix_length(track, genre, target_bars, fixes)
    _fix_parts(track, fixes)
    track.layout()
    return fixes


# -- globals -----------------------------------------------------------------


def _fix_globals(track: Track, genre: GenreProfile, fixes: list[str]) -> None:
    track.genre = genre.name
    lo, hi = genre.bpm_range
    if not lo <= track.bpm <= hi:
        new = min(max(track.bpm, lo), hi)
        fixes.append(f"tempo {track.bpm:g} BPM is outside {genre.name} ({lo}-{hi}), set to {new:g}")
        track.bpm = new
    track.bpm = round(track.bpm * 2) / 2
    if not 0 <= track.root <= 11:
        track.root %= 12
    if track.scale not in SCALES:
        fixes.append(f"unknown scale {track.scale!r}, using {genre.default_scale}")
        track.scale = genre.default_scale


# -- patterns ----------------------------------------------------------------


def _fix_pattern(p: Pattern, track: Track, genre: GenreProfile, fixes: list[str]) -> None:
    if p.bars not in PATTERN_BARS:
        new = min(PATTERN_BARS, key=lambda b: (abs(b - p.bars), b))
        fixes.append(f"{p.id}: {p.bars} bars is not a loop length, using {new}")
        p.bars = new

    kept: list[Note] = []
    dropped = 0
    for n in p.notes:
        if not 0 <= n.step < p.steps:
            dropped += 1
            continue
        n.length = max(1, min(int(n.length), p.steps - n.step))
        n.velocity = max(1, min(int(n.velocity), 127))
        kept.append(n)
    if dropped:
        fixes.append(f"{p.id}: removed {dropped} notes outside the {p.bars}-bar loop")
    p.notes = sorted(kept, key=lambda n: (n.step, n.pitch))

    info = p.role.info
    if info.drum:
        for n in p.notes:
            n.pitch = info.note
        if p.role is Role.KICK:
            _fix_kick(p, fixes)
        elif p.role is Role.OPEN_HAT:
            _fix_open_hat(p, fixes)
        p.notes = _dedupe(p.notes, by_pitch=False)
        return

    if p.role is Role.BASS:
        _fix_bass_grid(p, genre, fixes)
    moved = 0
    for n in p.notes:
        new = fold_into_range(snap_to_scale(n.pitch, track.root, track.scale), info.low, info.high)
        if new % 12 != n.pitch % 12:
            moved += 1
        n.pitch = new
    if moved:
        fixes.append(f"{p.id}: moved {moved} notes into {track.key}")
    p.notes = _dedupe(p.notes, by_pitch=not info.mono)
    if info.mono:
        _trim_overlaps(p)


def _dedupe(notes: list[Note], by_pitch: bool) -> list[Note]:
    """Keep the loudest note per step (or per step and pitch)."""
    best: dict[tuple, Note] = {}
    for n in notes:
        key = (n.step, n.pitch) if by_pitch else (n.step,)
        if key not in best or n.velocity > best[key].velocity:
            best[key] = n
    return sorted(best.values(), key=lambda n: (n.step, n.pitch))


def _trim_overlaps(p: Pattern) -> None:
    for a, b in zip(p.notes, p.notes[1:]):
        a.length = max(1, min(a.length, b.step - a.step))


def _fix_kick(p: Pattern, fixes: list[str]) -> None:
    off = [n for n in p.notes if n.step % STEPS_PER_BEAT]
    if off:
        fixes.append(f"{p.id}: removed {len(off)} kicks off the quarter-note grid")
        p.notes = [n for n in p.notes if not n.step % STEPS_PER_BEAT]
    for n in p.notes:
        n.length = 1


def _fix_open_hat(p: Pattern, fixes: list[str]) -> None:
    moved = 0
    for n in p.notes:
        offbeat = n.step - n.step % STEPS_PER_BEAT + 2
        if n.step != offbeat:
            n.step = offbeat
            moved += 1
    if moved:
        fixes.append(f"{p.id}: moved {moved} open hats onto the offbeat")


def _fix_bass_grid(p: Pattern, genre: GenreProfile, fixes: list[str]) -> None:
    allowed = set(genre.bass_offsets)
    clash = [n for n in p.notes if n.step % STEPS_PER_BEAT not in allowed]
    if clash:
        fixes.append(f"{p.id}: removed {len(clash)} bass notes that clash with the kick")
        p.notes = [n for n in p.notes if n.step % STEPS_PER_BEAT in allowed]
    cut = 0
    for n in p.notes:
        # A bass note must be over before the next kick hits.
        room = STEPS_PER_BEAT - n.step % STEPS_PER_BEAT
        if n.length > room:
            n.length = room
            cut += 1
    if cut:
        fixes.append(f"{p.id}: shortened {cut} bass notes that rang into the kick")


def _ensure_full_kick(p: Pattern, fixes: list[str]) -> None:
    have = {n.step for n in p.notes}
    missing = [s for s in range(0, p.steps, STEPS_PER_BEAT) if s not in have]
    if missing:
        vel = max((n.velocity for n in p.notes), default=120)
        p.notes = sorted(p.notes + [Note(s, Role.KICK.info.note, 1, vel) for s in missing], key=lambda n: n.step)
        fixes.append(f"{p.id}: filled {len(missing)} missing kicks so the drop runs four-on-the-floor")


# -- sections ----------------------------------------------------------------


def _pattern_for(track: Track, genre: GenreProfile, role: Role, fixes: list[str]) -> Pattern:
    """An existing pattern for ``role`` with notes, or a new default one."""
    for p in track.patterns.values():
        if p.role is role and p.notes:
            return p
    p = default_pattern(role, genre, track.root, track.scale)
    pid, i = p.id, 2
    while pid in track.patterns:
        pid, i = f"{p.id}{i}", i + 1
    p.id = pid
    track.patterns[pid] = p
    fixes.append(f"added a default {role.value} pattern")
    return p


def _fix_sections(track: Track, genre: GenreProfile, fixes: list[str]) -> None:
    phrase = genre.phrase_bars
    if not track.sections:
        fixes.append("no arrangement was given, using the genre template")
        for kind, bars, roles in genre.template:
            track.sections.append(Section(kind.value, kind, bars, energy=DEFAULT_ENERGY[kind]))
        for s, (_, _, roles) in zip(track.sections, genre.template):
            s.patterns = [_pattern_for(track, genre, r, fixes).id for r in roles]

    names: set[str] = set()
    for s in track.sections:
        snapped = max(phrase, round(s.bars / phrase) * phrase)
        if snapped != s.bars:
            fixes.append(f"{s.name}: {s.bars} bars rounded to {snapped} ({phrase}-bar phrases)")
            s.bars = snapped
        s.energy = max(1, min(int(s.energy), 10))
        refs: list[str] = []
        seen_roles: set[Role] = set()
        for pid in s.patterns:
            p = track.patterns.get(pid)
            if p is None:
                fixes.append(f"{s.name}: dropped unknown pattern {pid!r}")
            elif not p.notes:
                fixes.append(f"{s.name}: dropped empty pattern {pid!r}")
            elif p.role in seen_roles:
                fixes.append(f"{s.name}: dropped {pid!r}, {p.role.value} already has a pattern here")
            else:
                seen_roles.add(p.role)
                refs.append(pid)
        s.patterns = refs
        base, i = s.name or s.kind.value, 2
        while s.name in names or not s.name:
            s.name, i = f"{base} {i}", i + 1
        names.add(s.name)

    if not any(s.kind is SectionKind.DROP for s in track.sections):
        middle = [s for s in track.sections if s.kind not in (SectionKind.INTRO, SectionKind.OUTRO)]
        target = max(middle or track.sections, key=lambda s: s.bars)
        fixes.append(f"{target.name}: turned into a drop, the track had none")
        target.kind = SectionKind.DROP

    if track.sections[0].kind is not SectionKind.INTRO:
        intro = Section("intro", SectionKind.INTRO, 2 * phrase, energy=3)
        intro.patterns = [_pattern_for(track, genre, r, fixes).id for r in (Role.KICK, Role.HAT)]
        track.sections.insert(0, intro)
        fixes.append("added an intro at the start")
    if track.sections[-1].kind is not SectionKind.OUTRO:
        outro = Section("outro", SectionKind.OUTRO, 2 * phrase, energy=3)
        outro.patterns = [_pattern_for(track, genre, r, fixes).id for r in (Role.KICK, Role.HAT)]
        track.sections.append(outro)
        fixes.append("added an outro at the end")

    for s in track.sections:
        roles = track.section_patterns(s)
        if s.kind is SectionKind.DROP:
            for role in (Role.KICK, Role.BASS):
                if role not in roles:
                    s.patterns.append(_pattern_for(track, genre, role, fixes).id)
                    fixes.append(f"{s.name}: added the {role.value}, every drop needs one")
        elif s.kind is SectionKind.BREAKDOWN:
            gone = [p.id for r, p in roles.items() if r in (Role.KICK, Role.BASS)]
            if gone:
                s.patterns = [pid for pid in s.patterns if pid not in gone]
                fixes.append(f"{s.name}: removed kick and bass, breakdowns run without them")
        if s.kind is SectionKind.DROP:
            kick = track.section_patterns(s)[Role.KICK]
            _ensure_full_kick(kick, fixes)

    # Patterns that no section plays anymore are dead weight for the export.
    used = {pid for s in track.sections for pid in s.patterns}
    for pid in [pid for pid in track.patterns if pid not in used]:
        del track.patterns[pid]


def _fix_length(track: Track, genre: GenreProfile, target_bars: int | None, fixes: list[str]) -> None:
    phrase = genre.phrase_bars
    if target_bars:
        lo = max(phrase * 8, int(target_bars * 0.85))
        hi = int(target_bars * 1.15)
    else:
        lo, hi = genre.bar_range
    drops = [s for s in track.sections if s.kind is SectionKind.DROP]
    before = track.total_bars
    while track.total_bars < lo:
        min(drops, key=lambda s: s.bars).bars += phrase * 2
    while track.total_bars > hi:
        candidates = [s for s in track.sections if s.bars > (MIN_DROP_BARS if s.kind is SectionKind.DROP else phrase * 2)]
        if not candidates:
            break
        max(candidates, key=lambda s: s.bars).bars -= phrase
    if track.total_bars != before:
        fixes.append(f"length {before} bars adjusted to {track.total_bars} (aim {lo}-{hi})")


def _fix_parts(track: Track, fixes: list[str]) -> None:
    used = track.roles_used()
    for role in used:
        if role not in track.parts:
            track.parts[role] = Part(role)
    for role in [r for r in track.parts if r not in used]:
        del track.parts[role]
    track.parts = {r: track.parts[r] for r in used}

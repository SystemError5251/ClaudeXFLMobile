import pytest

from psygen.compose import GENRES, ComposeRequest, Note, Pattern, Role, Section, SectionKind, Track, fix_track, skeleton
from psygen.compose.model import STEPS_PER_BAR
from psygen.compose.theory import scale_pitch_classes

PSY = GENRES["psytrance"]


def make_track(**kw):
    track = Track(title="t", genre="psytrance", bpm=145, root=6, scale="phrygian")
    for k, v in kw.items():
        setattr(track, k, v)
    return track


def add(track, pid, role, bars, notes):
    track.patterns[pid] = Pattern(pid, role, bars, [Note(*n) for n in notes])


def basic_track():
    t = make_track()
    add(t, "k", Role.KICK, 1, [(0, 36), (4, 36), (8, 36), (12, 36)])
    add(t, "b", Role.BASS, 1, [(s, 30) for s in range(16) if s % 4])
    add(t, "p", Role.PAD, 4, [(0, 54, 16), (0, 61, 16)])
    t.sections = [
        Section("intro", SectionKind.INTRO, 32, ["k"]),
        Section("drop", SectionKind.DROP, 64, ["k", "b"]),
        Section("break", SectionKind.BREAKDOWN, 32, ["p", "k", "b"]),
        Section("drop 2", SectionKind.DROP, 96, ["k", "b"]),
        Section("outro", SectionKind.OUTRO, 32, ["k", "b"]),
    ]
    return t


def test_clean_track_only_loses_breakdown_kick():
    t = basic_track()
    fixes = fix_track(t, PSY)
    assert fixes == ["break: removed kick and bass, breakdowns run without them"]
    assert t.sections[2].patterns == ["p"]
    assert [s.start_bar for s in t.sections] == [0, 32, 96, 128, 224]


def test_tempo_and_scale():
    t = basic_track()
    t.bpm, t.scale = 170, "lydian"
    fixes = fix_track(t, PSY)
    assert t.bpm == 150 and t.scale == "phrygian"
    assert any("tempo" in f for f in fixes) and any("scale" in f for f in fixes)


def test_kick_grid_and_four_on_the_floor():
    t = basic_track()
    add(t, "k", Role.KICK, 1, [(0, 50, 3), (3, 36), (8, 36)])
    fixes = fix_track(t, PSY)
    steps = [n.step for n in t.patterns["k"].notes]
    assert steps == [0, 4, 8, 12]
    assert all(n.pitch == 36 and n.length == 1 for n in t.patterns["k"].notes)
    assert any("off the quarter-note grid" in f for f in fixes)
    assert any("four-on-the-floor" in f for f in fixes)


def test_bass_offbeats_scale_and_register():
    t = basic_track()
    add(t, "b", Role.BASS, 1, [(0, 30), (1, 32, 6), (2, 31), (2, 30, 1, 50), (5, 90), (6, 10)])
    fixes = fix_track(t, PSY)
    notes = t.patterns["b"].notes
    allowed = scale_pitch_classes(6, "phrygian")
    assert all(n.step % 4 for n in notes)  # nothing on the kick
    assert all(n.step % 4 + n.length <= 4 for n in notes)  # over before the next kick
    assert all(n.pitch % 12 in allowed and 28 <= n.pitch <= 52 for n in notes)
    assert [n.step for n in notes] == [1, 2, 5, 6]  # one note per step (mono)
    assert any("clash with the kick" in f for f in fixes)
    assert any("moved 2 notes into F# phrygian" in f for f in fixes)


def test_open_hat_offbeat():
    t = basic_track()
    add(t, "oh", Role.OPEN_HAT, 1, [(0, 1), (3, 1), (6, 1)])
    t.sections[1].patterns.append("oh")
    fix_track(t, PSY)
    assert [n.step for n in t.patterns["oh"].notes] == [2, 6]
    assert all(n.pitch == 46 for n in t.patterns["oh"].notes)


def test_mono_lead_overlaps_trimmed_pad_chords_kept():
    t = basic_track()
    add(t, "l", Role.LEAD, 1, [(0, 66, 8), (4, 68, 8)])
    t.sections[3].patterns.append("l")
    fix_track(t, PSY)
    lead = t.patterns["l"].notes
    assert lead[0].length == 4
    assert len(t.patterns["p"].notes) == 2


def test_pattern_and_note_bounds():
    t = basic_track()
    add(t, "l", Role.LEAD, 3, [(0, 66, 99, 300), (40, 66), (70, 66)])
    t.sections[1].patterns.append("l")
    fixes = fix_track(t, PSY)
    p = t.patterns["l"]
    assert p.bars == 2
    assert [n.step for n in p.notes] == [0]
    assert p.notes[0].length == 2 * STEPS_PER_BAR and p.notes[0].velocity == 127
    assert any("removed 2 notes outside" in f for f in fixes)


def test_sections_phrases_refs_and_structure():
    t = make_track()
    add(t, "b", Role.BASS, 1, [(1, 30)])
    add(t, "e", Role.ARP, 1, [])
    t.sections = [
        Section("a", SectionKind.BUILD, 13, ["b", "nope", "e"]),
        Section("a", SectionKind.BUILD, 70, ["b"]),
    ]
    fixes = fix_track(t, PSY)
    kinds = [s.kind for s in t.sections]
    assert kinds[0] is SectionKind.INTRO and kinds[-1] is SectionKind.OUTRO
    assert SectionKind.DROP in kinds
    assert all(s.bars % 8 == 0 for s in t.sections)
    assert len({s.name for s in t.sections}) == len(t.sections)
    lo, hi = PSY.bar_range
    assert lo <= t.total_bars <= hi
    drop = next(s for s in t.sections if s.kind is SectionKind.DROP)
    assert {Role.KICK, Role.BASS} <= set(t.section_patterns(drop))
    assert "e" not in t.patterns  # unused patterns are dropped
    assert any("unknown pattern 'nope'" in f for f in fixes)
    assert any("13 bars rounded to 16" in f for f in fixes)


def test_one_pattern_per_role():
    t = basic_track()
    add(t, "b2", Role.BASS, 1, [(2, 30)])
    t.sections[1].patterns.append("b2")
    fixes = fix_track(t, PSY)
    assert "b2" not in t.sections[1].patterns
    assert any("already has a pattern" in f for f in fixes)


def test_target_length():
    t = basic_track()
    fix_track(t, PSY, target_bars=400)
    assert 340 <= t.total_bars <= 460
    t = basic_track()
    fix_track(t, PSY, target_bars=128)
    assert t.total_bars <= 147


def test_parts_follow_used_roles():
    t = basic_track()
    fix_track(t, PSY)
    assert list(t.parts) == [Role.KICK, Role.BASS, Role.PAD]


@pytest.mark.parametrize("genre", list(GENRES))
def test_skeleton_is_already_clean(genre):
    result = skeleton(ComposeRequest(genre=genre, key="A", seed=1))
    track = result.track
    assert result.fixes == []
    assert fix_track(track, GENRES[genre]) == []
    for n in track.timeline(Role.KICK):
        assert n.step % 4 == 0
    for n in track.timeline(Role.BASS):
        assert n.step % 4 != 0


def test_timeline_repeats_and_clips():
    t = make_track()
    add(t, "l", Role.LEAD, 2, [(0, 66, 4), (30, 66, 8)])
    t.sections = [Section("x", SectionKind.DROP, 8, ["l"]), Section("y", SectionKind.OUTRO, 1, ["l"])]
    t.layout()
    notes = list(t.timeline(Role.LEAD))
    assert [n.step for n in notes if n.section == "x"] == [0, 30, 32, 62, 64, 94, 96, 126]
    assert [(n.step, n.length) for n in notes if n.section == "y"] == [(128, 4)]


def test_round_trip(tmp_path):
    t = basic_track()
    fix_track(t, PSY)
    t.parts[Role.KICK].sample = "/s/kick.wav"
    loaded = Track.load(t.save(tmp_path / "t.json"))
    assert loaded.to_dict() == t.to_dict()
    assert loaded.parts[Role.KICK].sample == "/s/kick.wav"
    assert list(loaded.timeline()) == list(t.timeline())

import pytest

from psygen.compose import theory


@pytest.mark.parametrize("text, pc", [
    ("F#", 6), ("Gb", 6), ("f#m", 6), ("A minor", 9), ("Am", 9), ("Bb", 10), ("Bbm", 10),
    ("E", 4), ("ebm", 3), ("C# maj", 1), ("", None), (None, None), ("H", None),
])
def test_parse_key(text, pc):
    assert theory.parse_key(text) == pc


def test_degree_to_pitch():
    # F# phrygian, root in octave 1 = F#1 (MIDI 30)
    assert theory.degree_to_pitch(0, 6, "phrygian", 1) == 30
    assert theory.degree_to_pitch(1, 6, "phrygian", 1) == 31  # flat second
    assert theory.degree_to_pitch(7, 6, "phrygian", 1) == 42
    assert theory.degree_to_pitch(-1, 6, "phrygian", 1) == 28  # E below


def test_snap_to_scale_and_fold():
    allowed = theory.scale_pitch_classes(9, "aeolian")  # A minor: no sharps
    for pitch in range(40, 80):
        assert theory.snap_to_scale(pitch, 9, "aeolian") % 12 in allowed
    assert theory.snap_to_scale(61, 9, "aeolian") == 60  # C# -> C (tie goes down)
    assert theory.fold_into_range(20, 28, 52) == 32
    assert theory.fold_into_range(70, 28, 52) == 46
    assert theory.pitch_name(60) == "C4"

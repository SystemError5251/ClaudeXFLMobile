import pytest

from psygen.samples.categorize import Category, categorize_name, parse_bpm, parse_key, tokenize


@pytest.mark.parametrize(
    "path, expected",
    [
        ("Kicks/PsyKick_03_Hard.wav", Category.KICK),
        ("Drums/BD_909.wav", Category.KICK),
        ("909kickhard.wav", Category.KICK),
        ("Drums/Hats/hat_closed_01.wav", Category.HAT_CLOSED),
        ("Open Hat 2.wav", Category.HAT_OPEN),
        ("Drums/OH_tight.wav", Category.HAT_OPEN),
        ("hihat open long.wav", Category.HAT_OPEN),
        ("Clap_Reverb.wav", Category.CLAP),
        ("snare_tight.wav", Category.SNARE),
        ("Ride_01.wav", Category.CYMBAL),
        ("Crash Long.wav", Category.CYMBAL),
        ("Percussion/Shaker 4.wav", Category.PERC),
        ("Bass/Rolling Bass A.wav", Category.BASS),
        ("PsyBassline_F#m_145bpm.wav", Category.BASS),
        ("KickBass_Loop_145.wav", Category.BASS),
        ("Synths/Acid Lead 303.wav", Category.LEAD),
        ("Pluck_Short_C.wav", Category.PLUCK),
        ("Arp Loop 145bpm Am.wav", Category.ARP),
        ("Pads/Warm Pad.wav", Category.PAD),
        ("Atmos/Dark Texture.wav", Category.ATMOS),
        ("FX/Riser 8 bars.wav", Category.FX),
        ("downlifter_noise.wav", Category.FX),
        ("Vocals/Spoken Word 1.wav", Category.VOCAL),
        ("Goa Stab Gm.wav", Category.SYNTH),
        ("Drum Loop 145.wav", Category.DRUMS),
        ("Loops/Top Loop 03.wav", Category.DRUMS),
        ("Drums/Kicks/Kick 1.wav", Category.KICK),
    ],
)
def test_categorize_by_name(path, expected):
    assert categorize_name(path).category is expected


def test_folder_supplies_category_when_filename_is_generic():
    guess = categorize_name("/samples/Pack/Kicks/001.wav", root="/samples")
    assert guess.category is Category.KICK
    assert 0 < guess.confidence < 1


def test_filename_beats_folder():
    guess = categorize_name("/samples/Drums/Kicks/clap_layer.wav", root="/samples")
    assert guess.category is Category.CLAP
    assert "kick" in guess.tags


def test_root_folder_name_is_ignored():
    guess = categorize_name("/sdcard/Bass Samples/001.wav", root="/sdcard/Bass Samples")
    assert guess.category is Category.UNKNOWN


def test_unknown_name():
    guess = categorize_name("recording_2024_01.wav")
    assert guess.category is Category.UNKNOWN
    assert guess.confidence == 0


def test_loop_detection_from_name():
    assert categorize_name("Drum Loop 145.wav").is_loop is True
    assert categorize_name("Bass_Am_145bpm.wav").is_loop is True
    assert categorize_name("Kick One Shot.wav").is_loop is False
    assert categorize_name("Kick 3.wav").is_loop is None


def test_tokenize():
    assert tokenize("PsyKick_03-Hard") == ["psy", "kick", "03", "hard"]
    assert tokenize("BD909") == ["bd", "909"]


@pytest.mark.parametrize(
    "text, bpm",
    [("Loop_145bpm", 145), ("loop 138 BPM", 138), ("loop-150-bpm", 150), ("no tempo", None), ("999bpm", None)],
)
def test_parse_bpm(text, bpm):
    assert parse_bpm(text) == bpm


@pytest.mark.parametrize(
    "text, key",
    [
        ("Bass_F#m_145", "F#m"),
        ("Lead Am", "Am"),
        ("Pad_Ebmin", "D#m"),
        ("Arp_Cmaj", "C"),
        ("Synth key A", "A"),
        ("FM Bass", None),
        ("Kick A", None),
    ],
)
def test_parse_key(text, key):
    assert parse_key(text) == key

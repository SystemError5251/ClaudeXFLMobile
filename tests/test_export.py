import os
import wave

import mido
import numpy as np
import pytest

from psygen import cli, config
from psygen.compose import ComposeRequest, Note, Part, Pattern, Role, Section, SectionKind, Track, skeleton
from psygen.export import ExportOptions, export_track, exporter, full_midi, part_midi, render
from psygen.export.midi import PPQ, SAMPLER_ROOT, TICKS_PER_STEP
from psygen.export.wavio import write_wav as write_out
from psygen.samples import Category, SampleLibrary
from psygen.samples.audio_io import load_audio
from psygen.samples.library import EXPORT_MARKER, Sample
from conftest import SR, kick, pad, t, write_wav

BPM = 150.0  # 16th = 0.1 s, handy for sample positions


def small_track(kick_path=None, bass_path=None) -> Track:
    track = Track(title="Test Track!", genre="psytrance", bpm=BPM, root=9, scale="aeolian")
    track.parts[Role.KICK] = Part(Role.KICK, kick_path)
    track.parts[Role.BASS] = Part(Role.BASS, bass_path)
    track.parts[Role.PAD] = Part(Role.PAD, None)
    track.patterns["k"] = Pattern("k", Role.KICK, 1, [Note(s, 36, 1, 127) for s in (0, 4, 8, 12)])
    track.patterns["b"] = Pattern("b", Role.BASS, 1, [Note(s, 33 + (s % 4 == 2) * 12, 1, 100) for s in range(16) if s % 4])
    track.patterns["p"] = Pattern("p", Role.PAD, 2, [Note(0, 57, 32, 80), Note(0, 60, 32, 80)])
    track.sections = [
        Section("Intro", SectionKind.INTRO, 2, ["k", "p"]),
        Section("Drop", SectionKind.DROP, 2, ["k", "b"]),
    ]
    track.layout()
    return track


# -- MIDI --------------------------------------------------------------------


def _notes(mid: mido.MidiFile, track_index: int = 0):
    now, out, ons = 0, [], {}
    for msg in mid.tracks[track_index]:
        now += msg.time
        if msg.type == "note_on" and msg.velocity:
            ons[msg.note] = (now, msg)
        elif msg.type in ("note_off", "note_on"):
            start, on = ons.pop(msg.note)
            out.append((start, now - start, on.note, on.channel, on.velocity))
    return out


def test_part_midi_matches_timeline():
    track = small_track()
    mid = part_midi(track, Role.BASS)
    assert mid.type == 0 and mid.ticks_per_beat == PPQ
    notes = _notes(mid)
    expected = list(track.timeline(Role.BASS))
    assert len(notes) == len(expected) == 2 * 12
    assert notes[0] == (expected[0].step * TICKS_PER_STEP, TICKS_PER_STEP, 33, 0, 100)
    assert notes[0][0] == 2 * 4 * PPQ + TICKS_PER_STEP  # drop starts at bar 3
    metas = [m for m in mid.tracks[0] if m.is_meta]
    tempo = next(m for m in metas if m.type == "set_tempo")
    assert mido.tempo2bpm(tempo.tempo) == pytest.approx(BPM)
    assert [m.text for m in metas if m.type == "marker"] == ["Intro", "Drop"]


def test_drums_on_sampler_root_in_parts_and_gm_in_full():
    track = small_track()
    kick_notes = _notes(part_midi(track, Role.KICK))
    assert len(kick_notes) == 16
    assert {n[2] for n in kick_notes} == {SAMPLER_ROOT}
    assert {n[3] for n in kick_notes} == {9}

    full = full_midi(track)
    assert full.type == 1
    assert [t.name for t in full.tracks] == ["Test Track!", "kick", "bass", "pad"]
    assert {n[2] for n in _notes(full, 1)} == {36}
    assert len(_notes(full, 3)) == 2  # pad chord, played once


def test_repeated_notes_turn_off_before_retrigger():
    track = small_track()
    mid = part_midi(track, Role.KICK)
    now, sounding = 0, set()
    for msg in mid.tracks[0]:
        now += msg.time
        if msg.type == "note_on" and msg.velocity:
            assert msg.note not in sounding, f"overlap at tick {now}"
            sounding.add(msg.note)
        elif msg.type == "note_off":
            sounding.discard(msg.note)


# -- audio rendering ---------------------------------------------------------


def test_wav_writer_round_trip(tmp_path):
    x = (0.5 * np.sin(2 * np.pi * 440 * t(0.2))).astype(np.float32)
    for bits in (16, 24):
        path = write_out(tmp_path / f"x{bits}.wav", x, SR, bits)
        y, sr = load_audio(path)
        assert sr == SR and len(y) == len(x)
        assert np.abs(y - x).max() < 1e-3
    stereo = write_out(tmp_path / "s.wav", np.stack([x, -x], axis=1), SR, 16)
    with wave.open(str(stereo)) as w:
        assert w.getnchannels() == 2 and w.getnframes() == len(x)
    with pytest.raises(ValueError):
        write_out(tmp_path / "bad.wav", x, SR, 8)


def _onsets(x, threshold=0.05):
    above = np.abs(x) > threshold
    return np.flatnonzero(above & ~np.roll(above, 1))


def test_kick_sample_lands_on_the_grid_and_is_choked(tmp_path):
    long_kick = np.concatenate([kick(0.4), 0.5 * np.ones(SR)])  # 1.4 s, longer than a beat
    path = write_wav(tmp_path / "Kick.wav", long_kick)
    track = small_track(kick_path=str(path))
    voice = render.make_voice(track, Role.KICK, SR, None, [])
    assert isinstance(voice, render.SampleVoice)
    stem = render.render_stem(track, Role.KICK, voice, render.total_samples(track, SR))
    beat = int(0.4 * SR)
    hits = [i * beat for i in range(16)]
    for h in hits:
        assert abs(stem[h + 10]) > 0.1
    # Choked: the sustained tail never builds up beyond one hit.
    assert np.abs(stem).max() <= 1.0 + 1e-6
    # The last hit rings out fully after the end of the arrangement.
    assert abs(stem[hits[-1] + int(1.2 * SR)]) > 0.3


def _pitch_hz(x, sr):
    """Frequency of a clean sine from its rising zero crossings."""
    rising = np.flatnonzero((x[:-1] < 0) & (x[1:] >= 0))
    return sr * (len(rising) - 1) / (rising[-1] - rising[0])


def test_melodic_sample_is_repitched_from_its_root(tmp_path):
    tone = 0.8 * np.sin(2 * np.pi * 110.0 * t(1.0))  # A2 = MIDI 45
    path = write_wav(tmp_path / "Bass.wav", tone)
    track = small_track(bass_path=str(path))
    voice = render.make_voice(track, Role.BASS, SR, None, [])
    assert voice.root == pytest.approx(45, abs=0.2)
    for pitch, hz in ((45, 110.0), (57, 220.0), (33, 55.0)):
        snd = voice.sound(pitch, int(0.1 * SR))
        assert _pitch_hz(snd, SR) == pytest.approx(hz, rel=0.03)
        # One 16th plus the short release, not the whole second of sample.
        assert len(snd) == int(0.1 * SR) + int(render.NOTE_RELEASE_SECONDS * SR)


def test_root_from_library_metadata_and_key_name():
    meta = Sample(path="/x/bass.wav", category=Category.BASS, confidence=1, source="name",
                  features={"pitch_hz": 220.0})
    assert render.guess_root(Role.BASS, meta, np.zeros(0, np.float32), SR) == pytest.approx(57)
    noisy = np.random.default_rng(0).uniform(-1, 1, SR).astype(np.float32)
    keyed = Sample(path="/x/lead F#.wav", category=Category.LEAD, confidence=1, source="name", key="F#")
    root = render.guess_root(Role.LEAD, keyed, noisy, SR)
    assert root % 12 == 6 and Role.LEAD.info.low <= root <= Role.LEAD.info.high


def test_loops_are_tempo_matched_and_resampled(tmp_path):
    path = write_wav(tmp_path / "fx loop 140bpm.wav", pad(0.75), sr=22050)  # 1.5 s at 22.05 kHz
    meta = Sample(path=str(path), category=Category.FX, confidence=1, source="name", is_loop=True, bpm=140.0)
    data = render.load_sample(str(path), SR, 150.0, meta)
    assert len(data) == pytest.approx(1.4 * SR, rel=0.01)  # 22.05 -> 44.1 kHz, then 140 -> 150 BPM
    assert np.abs(data).max() == pytest.approx(1.0)
    far = Sample(path=str(path), category=Category.FX, confidence=1, source="name", is_loop=True, bpm=100.0)
    assert len(render.load_sample(str(path), SR, 150.0, far)) == pytest.approx(1.5 * SR, rel=0.01)


def test_master_never_clips():
    rng = np.random.default_rng(1)
    mix = (rng.normal(0, 0.3, (2, SR)) * 5).astype(np.float32)
    render.master(mix)
    assert np.abs(mix).max() <= 0.97 + 1e-6
    assert np.sqrt((mix ** 2).mean()) > 0.2


# -- whole export --------------------------------------------------------------


def test_export_writes_everything(tmp_path):
    kick_path = write_wav(tmp_path / "in" / "Kick.wav", kick())
    track = small_track(kick_path=str(kick_path), bass_path=str(tmp_path / "missing.wav"))
    lines = []
    result = export_track(track, tmp_path / "out", ExportOptions(sample_rate=22050), progress=lines.append)

    folder = tmp_path / "out" / "test-track"
    assert result.folder == folder
    assert (folder / EXPORT_MARKER).exists()
    assert (folder / "test-track.mid").exists() and (folder / "track.json").exists()
    assert sorted(p.name for p in (folder / "midi").iterdir()) == ["01-kick.mid", "02-bass.mid", "11-pad.mid"]
    assert sorted(p.name for p in (folder / "stems").iterdir()) == ["01-kick.wav", "02-bass.wav", "11-pad.wav"]
    assert result.sources == {Role.KICK: "Kick.wav", Role.BASS: "built-in sound", Role.PAD: "built-in sound"}
    assert any("missing.wav" in w for w in result.warnings)
    assert Track.load(folder / "track.json").total_bars == 4

    lengths = set()
    for stem in result.stems.values():
        with wave.open(str(stem)) as w:
            assert w.getnchannels() == 1 and w.getframerate() == 22050
            lengths.add(w.getnframes())
    assert lengths == {render.total_samples(track, 22050)}
    with wave.open(str(result.preview)) as w:
        assert w.getnchannels() == 2 and w.getnframes() in lengths
    preview, _ = load_audio(result.preview)
    assert 0.3 < np.abs(preview).max() <= 1.0
    info = (folder / "info.txt").read_text()
    assert "150 BPM" in info and "A aeolian" in info and "Kick.wav" in info
    assert any("rendering kick" in line for line in lines)


def test_reexport_replaces_old_files_and_respects_foreign_folders(tmp_path):
    track = small_track()
    export_track(track, tmp_path, ExportOptions(sample_rate=22050, preview=False))
    del track.parts[Role.PAD]
    track.sections[0].patterns = ["k"]
    second = export_track(track, tmp_path, ExportOptions(sample_rate=22050, preview=False))
    assert second.folder == tmp_path / "test-track"
    assert sorted(p.name for p in (second.folder / "stems").iterdir()) == ["01-kick.wav", "02-bass.wav"]
    assert not (second.folder / "midi" / "11-pad.mid").exists()

    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "mine.txt").write_text("keep")
    track.title = "Other"
    third = export_track(track, tmp_path, ExportOptions(stems=False, preview=False))
    assert third.folder == tmp_path / "other-2"
    assert (tmp_path / "other" / "mine.txt").read_text() == "keep"


def test_midi_only_without_numpy(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "numpy_available", lambda: False)
    result = export_track(small_track(), tmp_path)
    assert result.part_midi and not result.stems and result.preview is None
    assert any("numpy" in w for w in result.warnings)


def test_skeleton_exports_with_built_in_sounds(tmp_path):
    track = skeleton(ComposeRequest(genre="goa", seed=3)).track
    track.sections = track.sections[:3]  # keep the test quick
    track.layout()
    result = export_track(track, tmp_path, ExportOptions(sample_rate=22050, stems=False))
    preview, _ = load_audio(result.preview)
    assert np.sqrt((preview ** 2).mean()) > 0.05


def test_scanner_skips_export_folders(tmp_path):
    root = tmp_path / "My Samples"
    write_wav(root / "Kicks" / "Kick_01.wav", kick())
    export_track(small_track(), root / "psygen", ExportOptions(sample_rate=22050))
    assert [s.name for s in SampleLibrary.scan([root], analyze=False)] == ["Kick_01.wav"]


def test_default_export_dir(tmp_path, monkeypatch, isolated_home):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(config, "SHARED_STORAGE_ROOTS", (shared,))
    assert config.default_export_dir() == shared / "Music" / "psygen"
    (shared / "FL Studio Mobile").mkdir()
    assert config.default_export_dir() == shared / "FL Studio Mobile" / "My Samples" / "psygen"
    (shared / "FLM User Files").mkdir()
    assert config.default_export_dir() == shared / "FLM User Files" / "My Samples" / "psygen"
    monkeypatch.setattr(config, "SHARED_STORAGE_ROOTS", (tmp_path / "nope",))
    assert config.default_export_dir() == config.data_dir() / "exports"
    assert config.Config(export_dir="~/x").resolved_export_dir() == isolated_home / "x"


def test_cli_exports_latest_track(tmp_path, isolated_home, capsys):
    track = small_track()
    track.save(config.tracks_dir() / "older.json")
    newer = small_track()
    newer.title = "Newest"
    newer.save(config.tracks_dir() / "newest.json")
    os.utime(config.tracks_dir() / "older.json", (1, 1))
    dest = tmp_path / "dest"
    assert cli.main(["export", "--dest", str(dest), "--sample-rate", "22050", "--no-stems"]) == 0
    out = capsys.readouterr().out
    assert "Exporting Newest" in out and "newest-preview.wav" in out
    assert (dest / "newest" / "newest.mid").exists()
    assert not (dest / "newest" / "stems").exists()

    assert cli.main(["export", str(config.tracks_dir() / "older.json"), "--dest", str(dest), "--midi-only"]) == 0
    assert (dest / "test-track" / "midi" / "01-kick.mid").exists()
    assert not list((dest / "test-track").glob("*.wav"))


def test_cli_compose_offline_and_export(tmp_path, isolated_home, capsys):
    dest = tmp_path / "dest"
    assert cli.main(["compose", "--offline", "--seed", "1", "--export", "--midi-only", "--dest", str(dest)]) == 0
    folders = list(dest.iterdir())
    assert len(folders) == 1 and (folders[0] / "midi").is_dir()


def test_cli_export_without_tracks(isolated_home, capsys):
    assert cli.main(["export"]) == 1
    assert "psygen compose" in capsys.readouterr().err

import json
import random

import pytest

from psygen import cli, config
from psygen.samples import Category, SampleLibrary
from psygen.samples import library as library_mod
from conftest import drum_loop, hat, kick, pad, write_wav


@pytest.fixture
def sample_tree(tmp_path):
    root = tmp_path / "Samples"
    write_wav(root / "Pack A" / "Kicks" / "Kick_01.wav", kick())
    write_wav(root / "Pack A" / "Kicks" / "Kick_02.wav", kick())
    write_wav(root / "Pack A" / "Hats" / "CH_01.wav", hat())
    write_wav(root / "Pack A" / "Loops" / "Drum Loop 145bpm.wav", drum_loop())
    write_wav(root / "Unsorted" / "track 07.wav", kick())  # name says nothing
    write_wav(root / "Unsorted" / "thing.wav", pad())
    write_wav(root / ".hidden" / "kick.wav", kick())
    write_wav(root / "Android" / "kick.wav", kick())
    (root / "notes.txt").write_text("not audio")
    return root


def test_scan_categorizes_and_skips(sample_tree):
    lib = SampleLibrary.scan([sample_tree])
    names = {s.name: s for s in lib}
    assert set(names) == {"Kick_01.wav", "Kick_02.wav", "CH_01.wav", "Drum Loop 145bpm.wav", "track 07.wav", "thing.wav"}
    assert names["Kick_01.wav"].category is Category.KICK
    assert names["Kick_01.wav"].source == "name"
    assert names["CH_01.wav"].category is Category.HAT_CLOSED
    assert names["Drum Loop 145bpm.wav"].is_loop
    assert names["Drum Loop 145bpm.wav"].bpm == 145
    assert names["track 07.wav"].category is Category.KICK
    assert names["track 07.wav"].source == "audio"
    assert names["thing.wav"].category is Category.PAD
    assert names["Kick_01.wav"].duration == pytest.approx(0.4, abs=0.01)


def test_scan_without_analysis(sample_tree):
    lib = SampleLibrary.scan([sample_tree], analyze=False)
    unknown = {s.name for s in lib.by_category(Category.UNKNOWN)}
    assert unknown == {"track 07.wav", "thing.wav"}
    assert all(s.features is None for s in lib)


def test_missing_root_is_ignored(tmp_path):
    assert len(SampleLibrary.scan([tmp_path / "nope"])) == 0


def test_save_load_roundtrip(sample_tree, tmp_path):
    lib = SampleLibrary.scan([sample_tree])
    path = lib.save(tmp_path / "idx" / "library.json")
    loaded = SampleLibrary.load(path)
    assert [s.to_dict() for s in loaded] == [s.to_dict() for s in lib]
    assert loaded.samples[0].category in Category


def test_load_bad_or_old_index(tmp_path):
    p = tmp_path / "library.json"
    p.write_text("{broken")
    assert len(SampleLibrary.load(p)) == 0
    p.write_text(json.dumps({"version": 0, "samples": [{"path": "x"}]}))
    assert len(SampleLibrary.load(p)) == 0


def test_rescan_reuses_unchanged_files(sample_tree, monkeypatch):
    first = SampleLibrary.scan([sample_tree])
    calls = []
    original = library_mod.build_sample
    monkeypatch.setattr(library_mod, "build_sample", lambda *a: calls.append(a[0].name) or original(*a))
    write_wav(sample_tree / "Pack A" / "Kicks" / "Kick_03.wav", kick())
    second = SampleLibrary.scan([sample_tree], previous=first)
    assert calls == ["Kick_03.wav"]
    assert len(second) == len(first) + 1


def test_pick_and_counts(sample_tree):
    lib = SampleLibrary.scan([sample_tree])
    rng = random.Random(1)
    assert lib.pick(Category.KICK, rng).category is Category.KICK
    assert lib.pick(Category.BASS, rng) is None
    assert lib.pick("kick", rng, loops=True) is None
    assert lib.counts()[Category.KICK] == 3
    assert [s.name for s in lib.search("loop")] == ["Drum Loop 145bpm.wav"]


def test_config_roundtrip_and_defaults(isolated_home):
    shared = isolated_home / "storage" / "shared" / "Samples"
    shared.mkdir(parents=True)
    monkey_roots = (isolated_home / "storage" / "shared",)
    cfg = config.Config()
    old = config.SHARED_STORAGE_ROOTS
    config.SHARED_STORAGE_ROOTS = monkey_roots
    try:
        assert cfg.resolved_sample_dirs() == [shared]
    finally:
        config.SHARED_STORAGE_ROOTS = old
    assert cfg.add_sample_dir("~/mysamples")
    assert not cfg.add_sample_dir("~/mysamples")
    cfg.save()
    loaded = config.Config.load()
    assert loaded.sample_dirs == [str(isolated_home / "mysamples")]


def test_cli_scan_and_list(sample_tree, isolated_home, capsys):
    assert cli.main(["scan", str(sample_tree), "--add"]) == 0
    out = capsys.readouterr().out
    assert "6 samples" in out
    assert config.index_path().exists()
    assert config.Config.load().sample_dirs == [str(sample_tree)]

    assert cli.main(["list", "-c", "kick"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 3

    assert cli.main(["scan"]) == 0  # uses the saved folder


def test_cli_list_empty(isolated_home, capsys):
    assert cli.main(["list"]) == 1

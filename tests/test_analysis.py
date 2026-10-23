import struct

import numpy as np
import pytest

from psygen.samples import analysis, audio_io
from psygen.samples.categorize import Category
from conftest import SR, drum_loop, hat, kick, pad, write_wav


def test_kick_features_and_classification():
    f = analysis.extract_features(kick(), SR)
    assert f.low_ratio > 0.5
    assert f.centroid_hz < 400
    assert analysis.classify_features(f)[0] is Category.KICK


def test_hat_classification():
    f = analysis.extract_features(hat(), SR)
    assert f.pitch_hz is None
    assert analysis.classify_features(f)[0] in (Category.HAT_CLOSED, Category.HAT_OPEN)


def test_pad_classification():
    f = analysis.extract_features(pad(), SR)
    assert f.pitch_hz is not None
    assert f.attack_s > 0.2
    assert analysis.classify_features(f)[0] is Category.PAD


def test_loop_detection():
    f = analysis.extract_features(drum_loop(bars=2), SR)
    assert f.onsets >= 6
    assert analysis.looks_like_loop(f)
    assert not analysis.looks_like_loop(analysis.extract_features(kick(), SR))
    assert analysis.classify_features(f)[0] is Category.DRUMS


def test_pitch_estimate_on_sine():
    tt = np.arange(SR) / SR
    f = analysis.extract_features((0.5 * np.sin(2 * np.pi * 110 * tt)).astype(np.float32), SR)
    assert f.pitch_hz == pytest.approx(110, rel=0.03)


def test_wave_fallback_without_soundfile(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_io, "sf", None)
    path = write_wav(tmp_path / "k.wav", kick(), width=3, channels=2)
    x, sr = audio_io.load_audio(path)
    assert sr == SR
    assert x.ndim == 1
    assert np.allclose(x, np.clip(kick(), -1, 1), atol=1e-4)


def test_max_seconds(tmp_path):
    path = write_wav(tmp_path / "p.wav", pad(3.0))
    x, sr = audio_io.load_audio(path, max_seconds=1.0)
    assert len(x) == sr


def test_unreadable_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_io.shutil, "which", lambda _: None)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(audio_io.AudioLoadError):
        audio_io.load_audio(bad)


def _float_wav(path, n, sr=48000):
    data = np.zeros(n, dtype="<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, sr, sr * 4, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", 4) + b"INFO"
    body += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_wav_duration_reads_float_and_extra_chunks(tmp_path):
    path = tmp_path / "f.wav"
    _float_wav(path, 24000)
    assert audio_io.wav_duration(path) == pytest.approx(0.5)
    assert audio_io.wav_duration(tmp_path / "missing.wav") is None

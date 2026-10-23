import wave
from pathlib import Path

import numpy as np
import pytest

SR = 44100


def write_wav(path: Path, x: np.ndarray, sr: int = SR, width: int = 2, channels: int = 1) -> Path:
    """Write integer PCM with the stdlib so tests don't need soundfile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.clip(x, -1, 1)
    if channels > 1:
        x = np.repeat(x[:, None], channels, axis=1)
    if width == 2:
        raw = (x * 32767).astype("<i2").tobytes()
    elif width == 3:
        ints = (x * 8388607).astype(np.int32).reshape(-1)
        b = ints.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
        raw = b.tobytes()
    else:
        raise ValueError(width)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(sr)
        w.writeframes(raw)
    return path


def t(seconds):
    return np.arange(int(SR * seconds)) / SR


def kick(seconds=0.4):
    tt = t(seconds)
    freq = 50 + 150 * np.exp(-tt * 40)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return 0.9 * np.sin(phase) * np.exp(-tt * 9)


def hat(seconds=0.08, seed=0):
    rng = np.random.default_rng(seed)
    noise = rng.uniform(-1, 1, int(SR * seconds))
    hp = np.diff(np.diff(noise, prepend=0), prepend=0) / 4
    return 0.8 * hp * np.exp(-t(seconds) * 60)


def pad(seconds=3.0):
    tt = t(seconds)
    env = np.minimum(1, tt / 0.8)
    return 0.3 * env * (np.sin(2 * np.pi * 220 * tt) + 0.5 * np.sin(2 * np.pi * 330 * tt))


def drum_loop(bars=1, bpm=145):
    beat = 60 / bpm
    out = np.zeros(int(SR * beat * 4 * bars))
    k = kick(0.3)
    for i in range(4 * bars):
        start = int(i * beat * SR)
        out[start:start + len(k)] += k[: len(out) - start]
    return out


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def no_claude_code(monkeypatch):
    """Tests never run a real Claude Code; tests that want one patch this back."""
    from psygen import ai

    monkeypatch.setattr(ai, "claude_code_path", lambda cfg=None: None)

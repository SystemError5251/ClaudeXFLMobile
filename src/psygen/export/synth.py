"""Built-in sounds for parts that have no sample.

These keep every stem and the preview audible when the library is small or
empty. They are deliberately simple and cheap: drums are a few lines of
noise and sine sweeps, melodic parts use band-limited wavetables, so a whole
track renders quickly on a phone. Everything is deterministic.
"""

from __future__ import annotations

from ..compose.model import Role
from ..samples.audio_io import np

TABLE_SIZE = 2048


def midi_to_hz(pitch: float) -> float:
    return 440.0 * 2.0 ** ((pitch - 69) / 12.0)


def _t(seconds: float, sr: int):
    return np.arange(int(seconds * sr), dtype=np.float32) / sr


def _noise(n: int, seed: int):
    return np.random.default_rng(seed).uniform(-1.0, 1.0, n).astype(np.float32)


def _highpass(x):
    return np.diff(x, prepend=x[:1]) * 0.5


def _smooth(x, width: int):
    """Box low-pass; good enough for noise colouring."""
    if width <= 1:
        return x
    return np.convolve(x, np.ones(width, dtype=np.float32) / width, mode="same")


def _sweep(t, start_hz: float, end_hz: float, rate: float, sr: int):
    freq = end_hz + (start_hz - end_hz) * np.exp(-t * rate)
    return np.sin(2 * np.pi * np.cumsum(freq) / sr)


def _normalize(x):
    peak = float(np.abs(x).max()) if len(x) else 0.0
    return (x / peak).astype(np.float32) if peak > 0 else x.astype(np.float32)


def drum(role: Role, sr: int):
    """One-shot sound for a drum or one-shot role, or ``None`` (vocals)."""
    seed = list(Role).index(role) + 1
    if role is Role.KICK:
        t = _t(0.38, sr)
        body = _sweep(t, 210.0, 46.0, 32.0, sr) * np.exp(-t * 7.5)
        click = _highpass(_noise(len(t), seed)) * np.exp(-t * 400)
        x = np.tanh(2.2 * (body + 0.3 * click))
    elif role is Role.HAT:
        t = _t(0.07, sr)
        x = _highpass(_highpass(_noise(len(t), seed))) * np.exp(-t * 70)
    elif role is Role.OPEN_HAT:
        t = _t(0.32, sr)
        x = _highpass(_highpass(_noise(len(t), seed))) * np.exp(-t * 11)
    elif role is Role.CLAP:
        t = _t(0.26, sr)
        noise = _highpass(_smooth(_noise(len(t), seed), 3))
        env = np.exp(-t * 18)
        for burst in (0.0, 0.011, 0.022):
            env = np.maximum(env, np.where(t >= burst, np.exp(-(t - burst) * 220), 0))
        x = noise * env
    elif role is Role.SNARE:
        t = _t(0.22, sr)
        x = 0.7 * np.sin(2 * np.pi * 190 * t) * np.exp(-t * 28)
        x += _highpass(_noise(len(t), seed)) * np.exp(-t * 18)
    elif role is Role.PERC:
        t = _t(0.22, sr)
        x = _sweep(t, 260.0, 170.0, 25.0, sr) * np.exp(-t * 16)
    elif role is Role.RIDE:
        t = _t(1.1, sr)
        metal = sum(np.sign(np.sin(2 * np.pi * f * t)) for f in (3120.0, 4270.0, 5380.0)) / 3
        x = (0.5 * metal + _highpass(_noise(len(t), seed))) * np.exp(-t * 4)
    elif role is Role.FX:
        t = _t(4.0, sr)
        p = t / t[-1]
        noise = _noise(len(t), seed)
        x = ((1 - p) * _smooth(noise, 24) * 3 + p * _highpass(noise)) * p ** 2
    elif role is Role.ATMOS:
        t = _t(6.0, sr)
        x = _smooth(_noise(len(t), seed), 64) * np.sin(np.pi * t / t[-1]) ** 2
    else:
        return None
    return _normalize(x)


def _table(harmonics: int, odd_only: bool = False):
    phase = np.arange(TABLE_SIZE, dtype=np.float64) / TABLE_SIZE
    out = np.zeros(TABLE_SIZE)
    for k in range(1, max(1, harmonics) + 1):
        if odd_only and k % 2 == 0:
            continue
        out += np.sin(2 * np.pi * k * phase) / k
    out = np.append(out, out[0])  # wrap point for interpolation
    return (out / np.abs(out).max()).astype(np.float32)


_TABLES: dict[tuple[int, bool], object] = {}


def _osc(freq: float, n: int, sr: int, cutoff: float, odd_only: bool = False):
    """Band-limited saw (or square) at ``freq`` with partials up to ``cutoff``."""
    harmonics = int(max(1, min(64, min(cutoff, sr * 0.45) // max(freq, 1.0))))
    key = (harmonics, odd_only)
    if key not in _TABLES:
        _TABLES[key] = _table(harmonics, odd_only)
    pos = (np.arange(n, dtype=np.float64) * (freq * TABLE_SIZE / sr)) % TABLE_SIZE
    return np.interp(pos, np.arange(TABLE_SIZE + 1), _TABLES[key]).astype(np.float32)


def _adsr(n: int, sr: int, attack: float, release: float, gate: int):
    """Linear attack, full sustain until ``gate`` samples, then linear release."""
    env = np.ones(n, dtype=np.float32)
    a = min(n, max(1, int(attack * sr)))
    env[:a] = np.linspace(0, 1, a, endpoint=False)
    r = max(1, int(release * sr))
    if gate < n:
        tail = np.linspace(1, 0, min(r, n - gate), dtype=np.float32)
        env[gate:gate + len(tail)] *= tail
        env[gate + len(tail):] = 0
    return env


def melodic(role: Role, pitch: int, seconds: float, sr: int):
    """One note of a melodic part, ``seconds`` long plus its release."""
    f = midi_to_hz(pitch)
    if role is Role.BASS:
        # Rolling psy bass: short, punchy, filter closing fast.
        n = int(min(seconds, 0.4) * sr) + int(0.01 * sr)
        t = np.arange(n, dtype=np.float32) / sr
        fenv = np.exp(-t * 28)
        x = fenv * _osc(f, n, sr, 2600) + (1 - fenv) * _osc(f, n, sr, 320)
        return x * np.exp(-t * 9) * _adsr(n, sr, 0.002, 0.01, n - int(0.01 * sr))
    if role is Role.ARP:
        n = int((seconds + 0.08) * sr)
        t = np.arange(n, dtype=np.float32) / sr
        fenv = np.exp(-t * 14)
        x = fenv * _osc(f, n, sr, 7000) + (1 - fenv) * _osc(f, n, sr, 900)
        return x * np.exp(-t * 6) * _adsr(n, sr, 0.002, 0.08, int(seconds * sr))
    if role is Role.LEAD:
        n = int((seconds + 0.15) * sr)
        x = 0.6 * _osc(f, n, sr, 5000) + 0.4 * _osc(f * 1.004, n, sr, 5000, odd_only=True)
        return x * _adsr(n, sr, 0.01, 0.15, int(seconds * sr))
    if role is Role.PAD:
        n = int((seconds + 0.8) * sr)
        x = sum(_osc(f * d, n, sr, 2200) for d in (0.996, 1.0, 1.004)) / 3
        return x * _adsr(n, sr, min(0.5, seconds / 2), 0.8, int(seconds * sr))
    raise ValueError(f"{role.value} is not a melodic role")

"""Light audio analysis: a handful of cheap features and a fallback classifier.

Everything here is designed to run on a phone in well under a second per
sample: only the first few seconds are decoded and all features come from a
single FFT plus a coarse envelope.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .audio_io import AudioLoadError, load_audio, np
from .categorize import Category

MAX_ANALYSIS_SECONDS = 8.0
LOOP_MIN_SECONDS = 1.5


@dataclass
class Features:
    duration: float
    sample_rate: int
    peak: float
    rms_db: float
    centroid_hz: float
    low_ratio: float  # energy share below 150 Hz
    high_ratio: float  # energy share above 5 kHz
    zcr: float  # zero crossings per second / sample rate
    attack_s: float  # time from start to peak envelope
    decay_s: float  # time from peak until envelope falls 30 dB
    onsets: int  # transient count, high for loops
    pitch_hz: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def _envelope(x, sr, hop_s=0.005):
    hop = max(1, int(sr * hop_s))
    n = len(x) // hop
    if n == 0:
        return np.array([float(np.abs(x).max()) if len(x) else 0.0]), hop
    frames = np.abs(x[: n * hop]).reshape(n, hop)
    return frames.max(axis=1), hop


def _count_onsets(env, hop, sr):
    if len(env) < 3 or env.max() <= 0:
        return 0
    db = 20 * np.log10(np.maximum(env / env.max(), 1e-6))
    rise = np.diff(db)
    # A transient is a jump of 9 dB+ within one hop, at least 60 ms apart.
    min_gap = max(1, int(0.06 * sr / hop))
    count, last = 0, -min_gap
    for i in np.flatnonzero((rise > 9.0) & (db[1:] > -30.0)):
        if i - last >= min_gap:
            count += 1
            last = i
    # The very first transient at t=0 has no rise in front of it.
    if db[0] > -12.0:
        count += 1
    return count


def _estimate_pitch(x, sr, start, fmin=30.0, fmax=1000.0):
    """Autocorrelation pitch estimate on a short window after the attack."""
    win = int(0.1 * sr)
    seg = x[start + int(0.02 * sr): start + int(0.02 * sr) + win]
    if len(seg) < win // 2 or np.abs(seg).max() < 1e-3:
        return None
    seg = seg - seg.mean()
    corr = np.fft.irfft(np.abs(np.fft.rfft(seg, n=2 * len(seg))) ** 2)[: len(seg)]
    lo, hi = int(sr / fmax), min(int(sr / fmin), len(corr) - 1)
    if hi <= lo or corr[0] <= 0:
        return None
    lag = lo + int(np.argmax(corr[lo:hi]))
    # Weak periodicity means noise (hats, claps, FX): no pitch.
    if corr[lag] / corr[0] < 0.5:
        return None
    return round(sr / lag, 1)


def extract_features(x, sr: int, duration: float | None = None) -> Features:
    if len(x) == 0:
        raise AudioLoadError("empty audio")
    peak = float(np.abs(x).max())
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    rms_db = 20 * np.log10(max(rms, 1e-9))

    n = min(len(x), 1 << 16)
    spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n))) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    total = float(spec.sum()) or 1e-12
    centroid = float((freqs * spec).sum() / total)
    low = float(spec[freqs < 150].sum() / total)
    high = float(spec[freqs > 5000].sum() / total)
    zcr = float(np.mean(np.abs(np.diff(np.signbit(x).astype(np.int8)))))

    env, hop = _envelope(x, sr)
    peak_i = int(np.argmax(env))
    attack = peak_i * hop / sr
    after = env[peak_i:]
    below = np.flatnonzero(after < env[peak_i] * 10 ** (-30 / 20))
    decay = (below[0] if len(below) else len(after)) * hop / sr

    return Features(
        duration=round(duration if duration is not None else len(x) / sr, 4),
        sample_rate=int(sr),
        peak=round(peak, 4),
        rms_db=round(float(rms_db), 2),
        centroid_hz=round(centroid, 1),
        low_ratio=round(low, 4),
        high_ratio=round(high, 4),
        zcr=round(zcr, 4),
        attack_s=round(attack, 4),
        decay_s=round(decay, 4),
        onsets=_count_onsets(env, hop, sr),
        pitch_hz=_estimate_pitch(x, sr, peak_i * hop),
    )


def analyze_file(path: str | Path, duration: float | None = None) -> Features:
    x, sr = load_audio(path, max_seconds=MAX_ANALYSIS_SECONDS)
    return extract_features(x, sr, duration=duration)


def looks_like_loop(f: Features) -> bool:
    return f.duration >= LOOP_MIN_SECONDS and f.onsets >= 4


def classify_features(f: Features) -> tuple[Category, float]:
    """Best guess from audio alone. Confidence is deliberately modest."""
    noisy = f.pitch_hz is None
    if looks_like_loop(f):
        if not noisy and f.pitch_hz < 200 and f.low_ratio > 0.3 and f.decay_s > 0.5:
            return Category.BASS, 0.3
        if noisy or f.high_ratio > 0.2 or f.low_ratio > 0.5:
            return Category.DRUMS, 0.3
        return Category.SYNTH, 0.25
    if f.duration <= 1.2:
        if f.low_ratio > 0.5 and f.centroid_hz < 400:
            if f.decay_s > 0.6 and not noisy and f.attack_s > 0.01:
                return Category.BASS, 0.4
            return Category.KICK, 0.55
        if f.high_ratio > 0.45 or f.centroid_hz > 6000:
            return (Category.HAT_CLOSED, 0.5) if f.decay_s < 0.15 else (Category.HAT_OPEN, 0.45)
        if noisy and f.centroid_hz > 1200:
            return Category.CLAP, 0.35
        if not noisy and f.pitch_hz < 200 and f.low_ratio > 0.3:
            return Category.BASS, 0.4
        if not noisy and f.decay_s < 0.4:
            return Category.PLUCK, 0.3
        return Category.PERC, 0.3

    if noisy and (f.high_ratio > 0.3 or f.zcr > 0.15):
        return (Category.CYMBAL, 0.35) if f.duration < 4 and f.attack_s < 0.02 else (Category.FX, 0.35)
    if not noisy and f.pitch_hz < 200 and f.low_ratio > 0.3:
        return Category.BASS, 0.35
    if f.attack_s > 0.2:
        return (Category.PAD, 0.35) if not noisy else (Category.ATMOS, 0.3)
    if noisy:
        return Category.FX, 0.3
    return Category.SYNTH, 0.3

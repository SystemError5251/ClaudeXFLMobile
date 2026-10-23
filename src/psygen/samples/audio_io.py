"""Audio decoding with graceful fallbacks for Termux.

Order of preference:
1. ``soundfile`` (libsndfile): WAV, FLAC, OGG, AIFF, and MP3 on newer builds.
2. Python's ``wave`` module: integer PCM WAV with no extra packages.
3. ``ffmpeg`` on PATH (``pkg install ffmpeg``): anything else, e.g. MP3, M4A.

All loaders return a mono float32 numpy array in [-1, 1] plus the sample rate.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - analysis is optional
    np = None

try:
    import soundfile as sf
except (ImportError, OSError):  # OSError: libsndfile missing
    sf = None


class AudioLoadError(Exception):
    pass


def numpy_available() -> bool:
    return np is not None


def _to_mono(data):
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32, copy=False)


def _load_soundfile(path: Path, max_seconds: float | None):
    info = sf.info(str(path))
    frames = -1 if max_seconds is None else int(max_seconds * info.samplerate)
    data, sr = sf.read(str(path), frames=frames, dtype="float32", always_2d=True)
    return _to_mono(data), int(sr)


def _load_wave(path: Path, max_seconds: float | None):
    try:
        with wave.open(str(path), "rb") as w:
            channels, width, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            n = w.getnframes()
            if max_seconds is not None:
                n = min(n, int(max_seconds * sr))
            raw = w.readframes(n)
    except (wave.Error, EOFError) as exc:
        raise AudioLoadError(f"{path.name}: {exc}") from exc

    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[: len(b) - len(b) % 3].reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioLoadError(f"{path.name}: unsupported sample width {width}")
    data = data[: len(data) - len(data) % channels].reshape(-1, channels)
    return _to_mono(data), sr


def _load_ffmpeg(path: Path, max_seconds: float | None, sr: int = 44100):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioLoadError(f"{path.name}: no decoder (install soundfile or ffmpeg)")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path)]
    if max_seconds is not None:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioLoadError(f"{path.name}: ffmpeg failed: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()
        raise AudioLoadError(f"{path.name}: ffmpeg failed: {msg[-1] if msg else 'no output'}")
    return np.frombuffer(proc.stdout, dtype="<f4").copy(), sr


def load_audio(path: str | Path, max_seconds: float | None = None):
    """Decode ``path`` to ``(mono_float32_array, sample_rate)``."""
    if np is None:
        raise AudioLoadError("numpy is required for audio analysis")
    path = Path(path)
    errors: list[str] = []
    if sf is not None:
        try:
            return _load_soundfile(path, max_seconds)
        except Exception as exc:  # libsndfile raises its own error types
            errors.append(str(exc))
    if path.suffix.lower() == ".wav":
        try:
            return _load_wave(path, max_seconds)
        except AudioLoadError as exc:
            errors.append(str(exc))
    try:
        return _load_ffmpeg(path, max_seconds)
    except AudioLoadError as exc:
        errors.append(str(exc))
    raise AudioLoadError("; ".join(errors))


def wav_duration(path: str | Path) -> float | None:
    """Duration of a WAV file from its header alone (no numpy needed).

    Handles WAVE_FORMAT_IEEE_FLOAT and extensible headers that ``wave``
    rejects, by reading the RIFF chunks directly.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            byte_rate = None
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    return None
                cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                if cid == b"fmt ":
                    fmt = f.read(size + (size & 1))
                    byte_rate = struct.unpack("<I", fmt[8:12])[0]
                elif cid == b"data":
                    if not byte_rate:
                        return None
                    return size / byte_rate
                else:
                    f.seek(size + (size & 1), 1)
    except (OSError, struct.error):
        return None

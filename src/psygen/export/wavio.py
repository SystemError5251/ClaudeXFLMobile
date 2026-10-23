"""Write PCM WAV files with the standard library (no soundfile needed)."""

from __future__ import annotations

import wave
from pathlib import Path

from ..samples.audio_io import np

CHUNK_FRAMES = 1 << 18  # convert in slices to keep memory flat on a phone


def _to_pcm(x, bits: int) -> bytes:
    x = np.clip(x, -1.0, 1.0)
    if bits == 16:
        return (x * 32767.0).astype("<i2").tobytes()
    if bits == 24:
        ints = (x * 8388607.0).astype("<i4")
        return ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    raise ValueError(f"unsupported bit depth {bits} (use 16 or 24)")


def write_wav(path: str | Path, data, sample_rate: int, bits: int = 16) -> Path:
    """Write ``data`` (frames, or frames x channels, float in [-1, 1])."""
    if bits not in (16, 24):
        raise ValueError(f"unsupported bit depth {bits} (use 16 or 24)")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    channels = 1 if data.ndim == 1 else data.shape[1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(int(sample_rate))
        for start in range(0, len(data), CHUNK_FRAMES):
            w.writeframes(_to_pcm(data[start:start + CHUNK_FRAMES].reshape(-1), bits))
    return path

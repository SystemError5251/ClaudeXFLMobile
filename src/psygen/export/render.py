"""Render parts to audio from the user's samples (or built-in sounds).

Every part becomes one mono stem that starts at bar 1, so all stems line up
when dropped into FL Studio Mobile. Stems carry the mix balance (role gain
with 6 dB headroom); the preview is the panned sum of the stems through a
soft limiter.

Drum and one-shot parts play their sample from the start on every hit and
are choked by the next hit. Melodic parts repitch the sample relative to its
detected root note by resampling (speed and pitch change together, like a
classic sampler) and stop at the note's end with a short release.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ..compose.model import STEPS_PER_BEAT, Role, Track
from ..compose.theory import parse_key
from ..samples import analysis
from ..samples.audio_io import AudioLoadError, load_audio, np
from ..samples.library import Sample, SampleLibrary
from . import synth

# Mix balance per role: (gain in dB, pan from -1 left to 1 right).
ROLE_MIX: dict[Role, tuple[float, float]] = {
    Role.KICK: (0.0, 0.0),
    Role.BASS: (-3.0, 0.0),
    Role.HAT: (-13.0, 0.25),
    Role.OPEN_HAT: (-12.0, -0.2),
    Role.CLAP: (-8.0, 0.0),
    Role.SNARE: (-9.0, 0.05),
    Role.PERC: (-12.0, -0.35),
    Role.RIDE: (-16.0, 0.35),
    Role.ARP: (-10.0, 0.2),
    Role.LEAD: (-8.0, -0.05),
    Role.PAD: (-15.0, 0.0),
    Role.ATMOS: (-17.0, 0.0),
    Role.FX: (-12.0, 0.0),
    Role.VOCAL: (-8.0, 0.0),
}
STEM_HEADROOM = 0.5  # -6 dB so overlapping hits rarely clip a stem
MAX_SAMPLE_SECONDS = 30.0
TAIL_SECONDS = 2.0  # room for releases and long one-shots after the last bar
CHOKE_FADE_SECONDS = 0.004
NOTE_RELEASE_SECONDS = 0.03


def db(gain_db: float) -> float:
    return 10.0 ** (gain_db / 20.0)


def resample(x, ratio: float):
    """Read ``x`` ``ratio`` times faster (linear interpolation)."""
    if abs(ratio - 1.0) < 1e-6 or len(x) < 2:
        return x
    n = int((len(x) - 1) / ratio)
    pos = np.arange(n, dtype=np.float64) * ratio
    return np.interp(pos, np.arange(len(x)), x).astype(np.float32)


def _fade_out(x, samples: int):
    samples = min(samples, len(x))
    if samples > 0:
        x[-samples:] *= np.linspace(1.0, 0.0, samples, dtype=np.float32)
    return x


def hz_to_midi(hz: float) -> float:
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def guess_root(role: Role, sample: Sample | None, data, sr: int) -> float:
    """MIDI pitch the sample plays at its own speed.

    Uses the measured pitch from the library (or measures it now), then a key
    in the file name placed in the part's register, then C in that register.
    """
    hz = None
    if sample is not None and sample.features:
        hz = sample.features.get("pitch_hz")
    if hz is None and len(data):
        try:
            hz = analysis.extract_features(data[: int(analysis.MAX_ANALYSIS_SECONDS * sr)], sr).pitch_hz
        except AudioLoadError:
            hz = None
    if hz:
        return hz_to_midi(hz)
    pc = parse_key(sample.key) if sample is not None and sample.key else None
    middle = (role.info.low + role.info.high) / 2
    pc = 0 if pc is None else pc
    # Closest pitch with this pitch class to the middle of the register.
    return float(pc + 12 * round((middle - pc) / 12))


class Voice:
    """Makes the sound of one note for a part. Results are cached because a
    psytrance part repeats the same few notes thousands of times."""

    def __init__(self, role: Role, sr: int):
        self.role = role
        self.sr = sr
        self.one_shot = role.info.drum
        self.choke = self.one_shot or role.info.mono
        self._cache: dict[tuple[int, int], object] = {}

    def sound(self, pitch: int, length_samples: int):
        key = (0, 0) if self.one_shot else (pitch, length_samples)
        if key not in self._cache:
            self._cache[key] = self._make(pitch, length_samples)
        return self._cache[key]

    def _make(self, pitch: int, length_samples: int):  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class SynthVoice(Voice):
    def _make(self, pitch, length_samples):
        if self.one_shot:
            x = synth.drum(self.role, self.sr)
            return np.zeros(0, dtype=np.float32) if x is None else x
        return synth.melodic(self.role, pitch, length_samples / self.sr, self.sr).astype(np.float32)

    def describe(self) -> str:
        if self.one_shot and synth.drum(self.role, 8000) is None:
            return "no sample (silent)"
        return "built-in sound"


class SampleVoice(Voice):
    def __init__(self, role: Role, sr: int, data, root: float, path: str):
        super().__init__(role, sr)
        self.data = data
        self.root = root
        self.path = path

    def _make(self, pitch, length_samples):
        if self.one_shot:
            return self.data
        x = resample(self.data, 2.0 ** ((pitch - self.root) / 12.0))
        release = int(NOTE_RELEASE_SECONDS * self.sr)
        if len(x) > length_samples + release:
            x = _fade_out(x[: length_samples + release].copy(), release)
        return x

    def describe(self) -> str:
        return Path(self.path).name


def load_sample(path: str, sr: int, track_bpm: float, meta: Sample | None):
    """Decode a sample at ``sr``, peak-normalized. Loops with a known tempo
    close to the track's are sped up or slowed down to fit."""
    data, file_sr = load_audio(path, max_seconds=MAX_SAMPLE_SECONDS)
    if file_sr != sr:
        data = resample(data, file_sr / sr)
    if meta is not None and meta.is_loop and meta.bpm:
        ratio = track_bpm / meta.bpm
        if 0.85 <= ratio <= 1.18:
            data = resample(data, ratio)
    peak = float(np.abs(data).max()) if len(data) else 0.0
    if peak <= 0:
        raise AudioLoadError(f"{Path(path).name}: silent")
    return (data / peak).astype(np.float32)


def make_voice(track: Track, role: Role, sr: int, library: SampleLibrary | None,
               warnings: list[str]) -> Voice:
    part = track.parts.get(role)
    path = part.sample if part else None
    if path:
        meta = next((s for s in library if s.path == path), None) if library else None
        try:
            data = load_sample(path, sr, track.bpm, meta)
        except (AudioLoadError, OSError) as exc:
            warnings.append(f"{role.value}: cannot use {Path(path).name} ({exc}); built-in sound instead")
        else:
            root = 60.0 if role.info.drum else guess_root(role, meta, data, sr)
            return SampleVoice(role, sr, data, root, path)
    return SynthVoice(role, sr)


def step_seconds(track: Track) -> float:
    return 60.0 / track.bpm / STEPS_PER_BEAT


def total_samples(track: Track, sr: int) -> int:
    return int(math.ceil((track.duration_seconds + TAIL_SECONDS) * sr))


def render_stem(track: Track, role: Role, voice: Voice, n_total: int):
    """Mono float32 stem for ``role``, ``n_total`` samples long, before mixing gain."""
    sr = voice.sr
    out = np.zeros(n_total, dtype=np.float32)
    per_step = step_seconds(track) * sr
    notes = sorted(track.timeline(role), key=lambda n: n.step)
    starts = [int(round(n.step * per_step)) for n in notes]
    fade = int(CHOKE_FADE_SECONDS * sr)
    for i, n in enumerate(notes):
        start = starts[i]
        if start >= n_total:
            break
        snd = voice.sound(n.pitch, int(round(n.length * per_step)))
        if not len(snd):
            continue
        if voice.choke and i + 1 < len(notes):
            room = starts[i + 1] - start
            if room <= 0:
                continue  # a later note at the same step wins
            if len(snd) > room:
                snd = _fade_out(snd[:room].copy(), fade)
        end = min(n_total, start + len(snd))
        out[start:end] += snd[: end - start] * (max(1, min(127, n.velocity)) / 127.0)
    return out


def apply_mix_gain(stem, role: Role, warnings: list[str]):
    """Scale a stem to its mix level; pull it down if it would clip."""
    stem *= db(ROLE_MIX[role][0]) * STEM_HEADROOM
    peak = float(np.abs(stem).max()) if len(stem) else 0.0
    if peak > 0.999:
        stem *= 0.999 / peak
        warnings.append(f"{role.value}: stem lowered by {20 * math.log10(peak / 0.999):.1f} dB to avoid clipping")
    return stem


def new_mix(n: int):
    """Stereo mix buffer, channels first so each channel is contiguous.
    ``mix.T`` is the ``(frames, 2)`` view the WAV writer expects."""
    return np.zeros((2, n), dtype=np.float32)


def pan_into(mix, stem, role: Role) -> None:
    """Add a mono stem to a ``new_mix`` buffer with constant-power panning."""
    angle = (ROLE_MIX[role][1] + 1.0) * math.pi / 4
    mix[0] += stem * math.cos(angle)
    mix[1] += stem * math.sin(angle)


def master(mix):
    """Soft-limit the mix in place: the loudest 0.05% of peaks are rounded
    off by tanh, the rest is just made louder. Never exceeds full scale."""
    flat = mix.reshape(-1)
    probe = np.abs(flat[::7])
    if not len(probe) or probe.max() <= 0:
        return mix
    ref = float(np.percentile(probe, 99.95)) or float(probe.max())
    for start in range(0, len(flat), 1 << 18):
        chunk = flat[start:start + (1 << 18)]
        np.tanh(chunk / ref, out=chunk)
        chunk *= 0.97
    return mix


@dataclass
class StemInfo:
    role: Role
    source: str  # sample file name or "built-in sound"
    notes: int

"""MIDI export: one file per part plus the full arrangement.

FL Studio Mobile imports standard MIDI files into a channel's piano roll, so
every part gets its own small file with the track's tempo and section
markers. Drum and one-shot parts are written on C5 (MIDI 60), the root of
FL Studio Mobile's sampler, so loading the part's sample into a sampler
channel and importing the file plays it at its own pitch. The full
arrangement file keeps the General MIDI drum notes on channel 10, so any GM
player renders it sensibly.
"""

from __future__ import annotations

from pathlib import Path

import mido

from ..compose.model import STEPS_PER_BEAT, Role, Track

PPQ = 96  # FL Studio's default resolution
TICKS_PER_STEP = PPQ // STEPS_PER_BEAT
SAMPLER_ROOT = 60  # C5 in FL Studio's naming


# An event is (tick, order, factory, type, fields). Messages are only built
# once their delta time is known: mido validates on every construction and
# a long track has tens of thousands of notes.
Event = tuple[int, int, type, str, dict]


def _events_to_track(events: list[Event], name: str) -> mido.MidiTrack:
    """Offs (order 0) sort before ons (order 1) at the same tick so repeated
    notes retrigger cleanly; meta events (negative order) come first."""
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    now = 0
    for tick, _, factory, kind, fields in sorted(events, key=lambda e: (e[0], e[1])):
        track.append(factory(kind, time=tick - now, **fields))
        now = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _meta_events(track: Track) -> list[Event]:
    meta = mido.MetaMessage
    events: list[Event] = [
        (0, -2, meta, "set_tempo", {"tempo": mido.bpm2tempo(track.bpm)}),
        (0, -2, meta, "time_signature", {"numerator": 4, "denominator": 4}),
    ]
    for s in track.sections:
        events.append((s.start_bar * 4 * PPQ, -1, meta, "marker", {"text": s.name}))
    return events


def _note_events(track: Track, role: Role, gm_drums: bool) -> list[Event]:
    info = role.info
    channel = info.midi_channel
    msg = mido.Message
    events: list[Event] = []
    for n in track.timeline(role):
        pitch = n.pitch if (gm_drums or not info.drum) else SAMPLER_ROOT
        velocity = max(1, min(127, int(n.velocity)))
        start = n.step * TICKS_PER_STEP
        end = start + max(1, n.length) * TICKS_PER_STEP
        events.append((start, 1, msg, "note_on", {"channel": channel, "note": pitch, "velocity": velocity}))
        events.append((end, 0, msg, "note_off", {"channel": channel, "note": pitch, "velocity": 0}))
    return events


def part_midi(track: Track, role: Role) -> mido.MidiFile:
    """A single-track (type 0) file with tempo, markers and one part's notes."""
    mid = mido.MidiFile(type=0, ticks_per_beat=PPQ)
    events = _meta_events(track) + _note_events(track, role, gm_drums=False)
    mid.tracks.append(_events_to_track(events, role.value))
    return mid


def full_midi(track: Track) -> mido.MidiFile:
    """A type 1 file: a conductor track, then one track per part."""
    mid = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    mid.tracks.append(_events_to_track(_meta_events(track), track.title))
    for role in track.roles_used():
        mid.tracks.append(_events_to_track(_note_events(track, role, gm_drums=True), role.value))
    return mid


def write_midi(track: Track, out_dir: Path, stem: str) -> tuple[Path, dict[Role, Path]]:
    """Write ``<stem>.mid`` and ``midi/NN-role.mid`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    full_path = out_dir / f"{stem}.mid"
    full_midi(track).save(str(full_path))
    parts: dict[Role, Path] = {}
    midi_dir = out_dir / "midi"
    midi_dir.mkdir(exist_ok=True)
    for role in track.roles_used():
        path = midi_dir / f"{part_file_name(role)}.mid"
        part_midi(track, role).save(str(path))
        parts[role] = path
    return full_path, parts


def part_file_name(role: Role) -> str:
    """``01-kick``: numbered in the fixed role order so folders sort sensibly."""
    return f"{list(Role).index(role) + 1:02d}-{role.value}"

# ClaudeXFLMobile

`psygen` is an automated psytrance / goa / progressive track generator that
runs in [Termux](https://termux.dev) on Android. Claude, through the Claude
API, does the musical decisions; psygen handles your samples, enforces the
genre rules and exports MIDI per part plus WAV stems and a preview that you
can load into FL Studio Mobile.

> Status: step 3 of 4. The sample library, composing and MIDI/WAV export
> are done; the full terminal UI comes next.

## Install on Termux

Install Termux from F-Droid or GitHub (the Play Store build is outdated), then:

```sh
pkg update && pkg upgrade
pkg install python git python-numpy ffmpeg libsndfile rust binutils

# Let Termux read your shared storage (Downloads, Music, FL Studio Mobile…)
termux-setup-storage

git clone https://github.com/SystemError5251/ClaudeXFLMobile.git
cd ClaudeXFLMobile
pip install -e .
pip install soundfile        # optional, see below
```

Then set your Claude API key (from the [Claude Console](https://platform.claude.com/)):

```sh
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc
source ~/.bashrc
```

Notes:

- **anthropic** (the Claude SDK) depends on `pydantic-core` and `jiter`,
  which are written in Rust. If pip finds no prebuilt wheel for your phone it
  compiles them, which is why `rust` and `binutils` are in the `pkg` line.
  That first install can take a while; later ones are quick.
- **numpy**: install it with `pkg install python-numpy`, not pip. Building it
  from source on a phone takes ages and often fails.
- **soundfile** (optional) reads FLAC/OGG/AIFF quickly via libsndfile. Without
  it, WAV files are read with the standard library and everything else goes
  through `ffmpeg`.
- **ffmpeg** (optional) decodes MP3/M4A/Opus and anything soundfile can't read.
- Without numpy, `psygen` still works but sorts samples by name only.

On a desktop (for development) a normal `pip install -e ".[dev]"` works.

## Sample library

Put your samples somewhere in shared storage. These folders are picked up
automatically when they exist:

- `~/storage/shared/Samples` (that is, `Internal storage/Samples`)
- `~/storage/shared/Music/Samples`
- `~/storage/shared/Download/Samples`
- `~/storage/shared/FL Studio Mobile/My Samples`

On Android 11 and newer, FL Studio Mobile's own folder under `Android/data`
is usually not readable by Termux. Copy or move your samples to one of the
folders above instead.

```sh
psygen dirs                          # show which folders will be scanned
psygen scan                          # scan auto-detected folders
psygen scan ~/storage/shared/MySamples --add   # scan and remember a folder
psygen scan --ai                     # also let Claude sort samples the names don't explain
psygen list                          # counts per category
psygen list -c kick                  # list all kicks
psygen list -s goa                   # search paths
```

The index is cached in `~/.cache/psygen/library.json`; rescans only re-read
new or changed files. Settings live in `~/.config/psygen/config.json`.

### How samples are categorized

Categories: `kick`, `snare`, `clap`, `hat_closed`, `hat_open`, `cymbal`,
`perc`, `drums`, `bass`, `lead`, `pluck`, `arp`, `pad`, `synth`, `atmos`,
`fx`, `vocal`, `unknown`.

1. **Names first.** Keywords in the filename (`Kick_03`, `CH_01`,
   `PsyBassline`, `Riser`) count most, then the parent folders (`Kicks/001.wav`).
   BPM (`145bpm`), key (`F#m`, `Amin`) and loop vs one-shot are read from
   names too.
2. **Audio as a fallback.** When the name says nothing, a quick analysis of
   the first few seconds (spectrum balance, brightness, pitch, envelope,
   transient count) makes a modest guess and detects loops. These matches
   are marked `audio` with a lower confidence in `psygen list`.
3. **Claude for the rest (optional).** `psygen scan --ai` sends the paths and
   measured features (never the audio) of samples that steps 1 and 2 could
   not place confidently to Claude, in batches of 150. These are marked
   `claude`, and samples Claude already sorted are not sent again.

The model defaults to `claude-opus-5-5`; change `claude_model` in
`~/.config/psygen/config.json` to use another.

## Composing a track

```sh
psygen compose                                   # psytrance, Claude picks the rest
psygen compose -g goa -i "sunrise on the beach, twisted 303 lead" --key F# --minutes 8
psygen compose -g progressive --bpm 134 --scale dorian
psygen compose --offline -g psytrance            # template track, no Claude call
psygen show ~/.local/share/psygen/tracks/forest-gate.json
```

Genres: `psytrance`, `goa`, `progressive`. Claude writes the whole track in
one request: tempo, key and scale, which of your indexed samples each part
uses, note patterns per part on a 16th grid (as scale degrees) and the
arrangement of intro, build, drop, breakdown and outro sections. Parts are
`kick`, `bass`, `hat`, `open_hat`, `clap`, `snare`, `perc`, `ride`, `arp`,
`lead`, `pad`, `atmos`, `fx` and `vocal`.

psygen then checks the answer against the genre and repairs what breaks it,
listing every change:

- tempo inside the genre range (psy and goa 138-150, progressive 130-140)
- melodic notes in the track's scale and in each part's register; bass, arp
  and lead are monophonic
- kick only on quarter notes, and four-on-the-floor in every drop
- bass never on a kick step and always over before the next kick
- open hats on the offbeat
- sections in whole 8-bar phrases, intro first, outro last, at least one
  drop; drops always have kick and bass, breakdowns never do
- total length in a sensible range for the genre (or near `--minutes`)

The result is saved as JSON in `~/.local/share/psygen/tracks/`; `psygen
export` turns it into MIDI and audio (or add `--export` to `compose`). Composing uses `claude-opus-5-5` at `high` effort
(set `compose_effort` in the config to `medium` or `low` for cheaper, faster
runs). A whole track is a long answer, so expect a minute or two.

## Exporting for FL Studio Mobile

```sh
psygen export                        # the newest composed track
psygen export ~/.local/share/psygen/tracks/forest-gate.json --mp3
psygen compose -g goa --export       # compose and export in one go
psygen export --midi-only            # no audio, takes a second
psygen export --dest ~/storage/shared/Download/psy --bits 24
```

Each track gets its own folder:

```
forest-gate/
  forest-gate.mid           whole arrangement, one MIDI track per part
  forest-gate-preview.wav   stereo mixdown (+ .mp3 with --mp3, needs ffmpeg)
  midi/01-kick.mid ...      one MIDI file per part
  stems/01-kick.wav ...     one WAV per part, all starting at bar 1
  info.txt                  tempo, key, arrangement, which sample each part uses
  track.json                the track itself, to export again later
```

Where it goes (`psygen dirs` shows it): `FL Studio Mobile/My Samples/psygen/`
when Termux can reach FL Studio Mobile's folder, so the stems show up in its
sample browser; otherwise `Music/psygen/` in shared storage, which you can
open from FL Studio Mobile's file browser. Set `export_dir` in
`~/.config/psygen/config.json` (or pass `--dest`) to use another folder.
`export_sample_rate` (44100) and `export_bits` (16) are there too. psygen
never scans its own export folders as samples.

In FL Studio Mobile:

- Set the project tempo to the track's BPM (it is in `info.txt`).
- **Audio:** put each stem from `stems/` on its own track at bar 1. All stems
  are the same length, so they stay in sync. Stems are mono and already at
  mix balance with 6 dB headroom; the preview is panned and limited.
- **MIDI:** load the part's sample (or any FL Studio Mobile instrument) into a
  channel and import `midi/NN-part.mid` into its piano roll. Drum and one-shot
  parts are written on C5, so a sampler plays them at their own pitch. The
  MIDI files carry the tempo and a marker per section.
- The full `.mid` file uses General MIDI drum notes on channel 10, so it also
  plays sensibly in other apps.

How the audio is made: drum and one-shot parts play your sample on every hit,
cut off by the next hit. Melodic parts (bass, arp, lead, pad) repitch the
sample from its detected root note, like a classic sampler, so speed and
pitch change together. Loops with a tempo in the name are sped up or slowed
down to the track's tempo when they are within about 15%. Parts without a
sample (or whose file is gone) use simple built-in sounds, so the preview is
always complete; `info.txt` lists which part used what. Without numpy only
the MIDI files are written.

Stems are full length, so an 8-minute track is about 40 MB per stem at
44.1 kHz / 16 bit. Use `--no-stems` if you only want the preview and MIDI.

## Development

```sh
pip install -e ".[dev]"
pytest
```

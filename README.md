# ClaudeXFLMobile

`psygen` is an automated psytrance / goa / progressive track generator that
runs in [Termux](https://termux.dev) on Android. It builds tracks from your own
samples and exports MIDI per part plus WAV stems and a preview that you can
load into FL Studio Mobile.

> Status: step 1 of 4. The sample library is done; genre engines,
> arrangement, export and the full terminal UI come next.

## Install on Termux

Install Termux from F-Droid or GitHub (the Play Store build is outdated), then:

```sh
pkg update && pkg upgrade
pkg install python git python-numpy ffmpeg libsndfile

# Let Termux read your shared storage (Downloads, Music, FL Studio Mobile…)
termux-setup-storage

git clone https://github.com/SystemError5251/ClaudeXFLMobile
cd ClaudeXFLMobile
pip install -e .
pip install soundfile        # optional, see below
```

Notes:

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

## Development

```sh
pip install -e ".[dev]"
pytest
```

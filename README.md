# sonos

A single-file command-line controller for Sonos speakers — discovery, playback,
volume, grouping, text-to-speech, queue management, Apple Music search/playback,
and a personal "saved tracks + local playlists" library.

Created by **Mark Burnett** — [www.linkedin.com/in/markburnett](https://www.linkedin.com/in/markburnett) — [github.com/embernet/sonos](https://github.com/embernet/sonos)

## Features

- **Discover & control** — list speakers and groups, play/pause/stop/next/prev.
- **Volume** — absolute (`40`), relative (`+5`, `-5`), show all speakers' volumes.
- **Now playing** — rich `--status` (enriched with catalogue details), plus an
  all-speaker dashboard.
- **Grouping** — group rooms, whole-home "party" mode, ungroup.
- **Sleep timer**, **text-to-speech** (`--say`), and **house-wide announcements**
  (`--broadcast`).
- **Music search & playback** — local library and **Apple Music** (no login
  required), with type filters and an interactive picker.
- **Queue** — view, add, clear.
- **Saved tracks & local playlists** — remember songs you like as you hear them
  and build named playlists.
- **Convenience** — a remembered default speaker, a today-only temporary default,
  and saved "favourites" (one-shot scenes).
- **Headless-friendly** — works on a Raspberry Pi where multicast discovery
  fails, via a remembered speaker list and a seed IP.

## Requirements

- Python 3.7+
- [`soco`](https://github.com/SoCo/SoCo): `pip install soco`
- Network access to your Sonos system (no Sonos/Apple account login needed —
  Sonos treats anything on the network as authorised).

## Install

```bash
pip install soco
chmod +x sonos.py
```

Run it directly (`./sonos.py ...`) or install a global `sonos` command:

```bash
ln -sf "$(pwd)/sonos.py" ~/.local/bin/sonos
# ensure ~/.local/bin is on PATH (add to ~/.zshrc if needed):
#   export PATH="$HOME/.local/bin:$PATH"
```

Because it's a symlink, `sonos` always runs the latest version of the script.

## First-time setup

```bash
sonos --list                      # see your speakers and groups
sonos --speaker "Kitchen" --default          # remember a default speaker
sonos --speaker "Kitchen" --source "Apple Music" --default   # and a default source
```

### Raspberry Pi / headless

Multicast discovery is unreliable on the Pi. Two robust options:

```bash
# On a Mac (where discovery works), snapshot the speakers, then copy the config:
sonos --list --remember
scp ~/.config/sonos-cli/config.json pi@raspberrypi.local:~/.config/sonos-cli/config.json

# Or register a known speaker by name + IP directly:
sonos --seed-ip "Kitchen" 192.168.1.114
```

## Usage

### Speakers & defaults

```bash
sonos --list                       # speakers + current groups
sonos --list --remember            # also save them to config (fixed-IP friendly)
sonos --speaker "Office" --status  # target one speaker for a command
sonos --speaker all --stop         # apply a command to every speaker
sonos --default                    # show the default speaker, source, temp default
sonos --speaker "Kitchen" --default        # set the default speaker
sonos --speaker "Gazebo" --temp            # default for the rest of today only
sonos --temp                       # clear today's temporary default early
```

### Playback & volume

```bash
sonos --play                       # resume
sonos --pause / --stop / --next / --prev
sonos --volume 40                  # absolute
sonos --volume +5                  # relative up
sonos --volume=-5                  # relative down (note the '=')
sonos --volume                     # show every speaker's volume
sonos --status                     # detailed now-playing
sonos --status-all                 # one-line now-playing for all speakers
sonos --sleep 30                   # stop this speaker in 30 min (0 cancels)
```

### Grouping

```bash
sonos --speaker "Kitchen" --group "Conservatory,Living Room"   # sync rooms
sonos --speaker "Kitchen" --party                              # group ALL speakers
sonos --speaker "Conservatory" --ungroup                       # break one out
```

### Music search & playback

The search source is `Library` (local) by default, or a streaming service like
`Apple Music`. Set a default source with `--default`, or pass `--source` per call.

```bash
sonos --source                              # list available sources
sonos --play "Abbey Road"                   # auto: album then song
sonos --source "Apple Music" --album --play "Abbey Road"
sonos --source "Apple Music" --song  --play "Come Together"
sonos --play "Jazz" --playlist              # local / Sonos playlists
```

Type filters (mutually exclusive): `--album`, `--artist`, `--song`,
`--station`, `--playlist`. (Apple Music supports album and song.)

Interactive picker — list all matches and choose one:

```bash
sonos --query "Beatles"            # number = play; append 'q' to queue (e.g. 3q)
```

### Queue

```bash
sonos --queue                      # show the queue
sonos --queue "Abbey Road"         # add to the queue
sonos --clearqueue
```

### Speech

```bash
sonos --speaker "Kitchen" --say "Dinner is ready"     # speak on one speaker
sonos --broadcast "Time to go"                        # speak on ALL speakers
```

### Saved tracks & local playlists

A personal library kept in `~/.config/sonos-cli/saved.json`, separate from
settings.

```bash
sonos --save                       # save the currently-playing track
sonos --save "Chill"               # save it straight into playlist "Chill"
sonos --saved                      # list saved tracks; pick one to play/queue
sonos --remove                     # remove the last saved track
sonos --remove 3                   # remove saved track #3 (numbers from --saved)

sonos --makeplaylist "Chill"       # snapshot all saved tracks into a playlist
sonos --addtoplaylist "Chill"      # pick saved tracks by CSV to add to a playlist
sonos --playlists                  # list local playlists; pick one to play
sonos --playlists "Chill"          # play a playlist directly
sonos --delplaylist "Chill"        # delete a playlist
```

### Favourites (saved scenes)

```bash
sonos --speaker "Kitchen" --source "Apple Music" --album --play "Abbey Road" --favourite "Beatles"
sonos --favourite "Beatles"        # run a saved favourite by name
sonos --favourite 1                # ...or by number
sonos --favourite                  # list favourites and pick one
```

## How Apple Music works

There is **no login**. The tool searches the public **iTunes Search API** (the
same catalogue as Apple Music) and plays results through your **attached Apple
Music service** using Sonos's own URI scheme
(`x-sonosapi-hls-static:song%3a{id}?sid=204&flags=8232&sn=N`). The account serial
(`sn`) is captured from real playback on your household. Albums are enqueued as
their individual tracks. These values are overridable in `config.json`:

```json
{ "apple": { "sn": 6, "flags": 8232 } }
```

## Configuration files

Both live in `~/.config/sonos-cli/`:

- **`config.json`** — default speaker/source, temporary default, remembered
  speakers (name + IP), seed IP, favourites, and Apple Music overrides.
- **`saved.json`** — your saved tracks and local playlists.

### Diagnostics & maintenance

```bash
sonos --dump            # raw URI/metadata of what's playing (capture real values)
sonos --dumpqueue       # raw DIDL of the queue (shows service tokens)
sonos --clear           # clear only the remembered speaker list
sonos --reset           # clear all settings
```

## Notes

- Relative volume down must use `=`: `--volume=-5` (otherwise the shell reads
  `-5` as a flag).
- `--speaker all` is for control/playback actions, not the interactive pickers.
- Speaker names match case- and apostrophe-insensitively (`Mark's Study`).

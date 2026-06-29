#!/usr/bin/env python3
"""
sonos.py — Control Sonos speakers from the command line.
Created by Mark Burnett
License: MIT
"""

import argparse
import datetime
import json
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import soco
from soco.snapshot import Snapshot

CONFIG_DIR = Path.home() / ".config" / "sonos-cli"
CONFIG_FILE = CONFIG_DIR / "config.json"
# Separate from config: a growable store for saved tracks (and, later, local
# playlists etc.). Kept apart so settings and saved music don't get tangled.
SAVE_FILE = CONFIG_DIR / "saved.json"

# Sentinels for flags that take an OPTIONAL value: --volume alone shows all
# volumes, --play alone resumes playback.
VOL_SHOW = "\x00show"
QUEUE_SHOW = "\x00queueshow"
PLAY_RESUME = "\x00resume"
FAV_LIST = "\x00favlist"
SOURCE_LIST = "\x00sourcelist"
PLAYLISTS_LIST = "\x00playlists"
REMOVE_LAST = "\x00removelast"
SAVE_LIST = "\x00savelist"

# The action options a favourite captures (everything except meta/admin flags).
SCENE_KEYS = ("speaker", "source", "stype", "group", "party", "ungroup",
              "clearqueue", "queue", "play", "volume", "pause", "stop", "next",
              "prev", "sleep", "say", "broadcast", "status")
# Keys that count as a real "do something" action; speaker/source/stype modify.
ACTION_KEYS = tuple(k for k in SCENE_KEYS
                    if k not in ("speaker", "source", "stype"))

# Headless/Pi locales often default stdout to latin-1 or ASCII, which can't
# encode curly apostrophes and similar characters that appear in speaker
# names. Force UTF-8 so printing those names doesn't raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    print(f"Config file updated: {CONFIG_FILE}")


# --------------------------------------------------------------------------- #
# Saved-tracks store (separate file; structured for future features)
# --------------------------------------------------------------------------- #
def load_saved_data():
    if SAVE_FILE.exists():
        try:
            return json.loads(SAVE_FILE.read_text())
        except Exception:
            return {}
    return {}


def write_saved_data(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SAVE_FILE.write_text(json.dumps(data, indent=2))


def _current_track_entry(sp):
    """Build a save entry for whatever sp is currently playing, enriched from
    the Apple Music catalogue when Sonos's now-playing metadata is sparse.
    Returns the entry dict, or None if nothing is playing."""
    track = sp.get_current_track_info()
    title = track.get("title") or ""
    artist = track.get("artist") or ""
    album = track.get("album") or ""
    uri = track.get("uri") or ""
    m = re.search(r"song(?:%3[aA]|:)(\d+)", uri)
    if m and not (title and artist):
        try:
            d = _itunes_lookup_track(m.group(1))
        except Exception:
            d = None
        if d:
            title = title or d.get("trackName", "")
            artist = artist or d.get("artistName", "")
            album = album or d.get("collectionName", "")
    if not (title or uri):
        return None
    return {
        "title": title, "artist": artist, "album": album, "uri": uri,
        "speaker": sp.player_name,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def save_current_track(sp):
    """Append the track currently playing on sp to the saved list."""
    entry = _current_track_entry(sp)
    if not entry:
        print(f"Nothing is playing on {sp.player_name} to save.")
        return
    data = load_saved_data()
    items = data.setdefault("saved", [])
    items.append(entry)
    write_saved_data(data)
    sub = f" — {entry['artist']}" if entry["artist"] else ""
    extra = f"  [{entry['album']}]" if entry["album"] else ""
    print(f"Saved #{len(items)}: {entry['title'] or '(unknown)'}{sub}{extra}")
    print(f"  from {sp.player_name} at {entry['saved_at']}")
    print(f"  file: {SAVE_FILE}")


def save_current_to_playlist(sp, name):
    """Append the track currently playing on sp straight to a named local
    playlist (creating it if needed)."""
    entry = _current_track_entry(sp)
    if not entry:
        print(f"Nothing is playing on {sp.player_name} to save.")
        return
    data = load_saved_data()
    pls = data.setdefault("playlists", {})
    match = next((k for k in pls if _normalize(k) == _normalize(name)), None)
    created = match is None
    if created:
        pls[name] = []
        match = name
    pls[match].append(entry)
    write_saved_data(data)
    sub = f" — {entry['artist']}" if entry["artist"] else ""
    action = "Created" if created else "Added to"
    print(f"{action} playlist '{match}': {entry['title'] or '(unknown)'}{sub}")
    print(f"  from {sp.player_name}; {len(pls[match])} track(s) in playlist.")


def remove_saved(which):
    """Remove a saved track: the last one (which == REMOVE_LAST) or the Nth
    as numbered by --saved."""
    data = load_saved_data()
    items = data.get("saved", [])
    if not items:
        print("Nothing saved to remove.")
        return
    if which == REMOVE_LAST:
        idx = len(items) - 1
    elif str(which).isdigit() and 1 <= int(which) <= len(items):
        idx = int(which) - 1
    else:
        print(f"Invalid saved number '{which}'. Use --saved to see the numbers.")
        return
    removed = items.pop(idx)
    write_saved_data(data)
    sub = f" — {removed.get('artist')}" if removed.get("artist") else ""
    print(f"Removed #{idx + 1}: {removed.get('title', '(unknown)')}{sub}")
    print(f"  {len(items)} saved track(s) remaining.")


# --------------------------------------------------------------------------- #
# Speaker discovery / resolution
# --------------------------------------------------------------------------- #
def _speakers_from_seed(ip):
    # Connect to one known speaker and ask it for the whole household, so we
    # never depend on discovery. visible_zones = controllable rooms (excludes
    # bonded satellites/subs); fall back to all_zones, then the seed itself.
    try:
        seed = soco.SoCo(ip)
        return seed.visible_zones or seed.all_zones or {seed}
    except Exception:
        return set()


def remembered_speakers():
    """Build SoCo objects from the saved name->IP list (no discovery needed)."""
    speakers = set()
    for entry in load_config().get("speakers") or []:
        ip = entry.get("ip")
        if ip:
            try:
                speakers.add(soco.SoCo(ip))
            except Exception:
                pass
    return speakers


def live_discover():
    """Find speakers on the network: multicast -> subnet scan -> seed IP."""
    # Multicast SSDP (soco.discover) is unreliable on a Pi — Wi-Fi/IGMP often
    # drops the replies, so retry a few times with a generous timeout.
    for _ in range(3):
        speakers = soco.discover(timeout=10)
        if speakers:
            return speakers
    # Still nothing: fall back to a direct unicast scan of the subnet, which
    # doesn't depend on multicast at all (slower, but reliable on the Pi).
    try:
        from soco import discovery as _discovery
        speakers = _discovery.scan_network(multi_household_check=False)
        if speakers:
            return speakers
    except Exception:
        pass
    # Last resort: a known speaker IP from config. One reachable speaker is
    # enough to enumerate the entire household, skipping discovery completely.
    seed = load_config().get("seed_ip")
    if seed:
        speakers = _speakers_from_seed(seed)
        if speakers:
            return speakers
    return set()


def discover():
    """Speakers for normal operations: prefer the saved list (fixed IPs, fast
    and reliable, no multicast), falling back to live network discovery."""
    return remembered_speakers() or live_discover()


def _normalize(s):
    # Fold the various curly/straight apostrophe characters to a plain "'"
    # so a name typed on the command line matches the speaker's actual name.
    for ch in ("‘", "’", "ʼ", "′", "`", "´"):
        s = s.replace(ch, "'")
    return s.strip().lower()


def find_speaker(name):
    target = _normalize(name)
    # Prefer the saved name->IP map: match by stored name, connect directly,
    # no network query or discovery needed.
    for entry in load_config().get("speakers") or []:
        if entry.get("ip") and _normalize(entry.get("name", "")) == target:
            return soco.SoCo(entry["ip"])
    # Otherwise fall back to live discovery and match player names.
    for s in live_discover():
        if _normalize(s.player_name) == target:
            return s
    return None


def ip_for_name(name):
    """Best-effort IP for a speaker name: saved list first, then discovery."""
    target = _normalize(name)
    for entry in load_config().get("speakers") or []:
        if _normalize(entry.get("name", "")) == target:
            return entry.get("ip")
    sp = find_speaker(name)
    return sp.ip_address if sp else None


def effective_default_name(cfg):
    """Active default speaker: a --temp override if it's still today, else the
    base default. An expired temp override is cleared automatically."""
    temp = cfg.get("temp_default")
    if temp:
        if temp.get("date") == datetime.date.today().isoformat():
            return temp.get("speaker")
        cfg.pop("temp_default", None)
        save_config(cfg)
        print(f"Temporary default expired; reverted to "
              f"'{cfg.get('default_speaker') or '(none)'}'.")
    return cfg.get("default_speaker")


def resolve_speaker(args, cfg):
    name = args.speaker or effective_default_name(cfg)
    if not name:
        sys.exit("No speaker given and no default set. "
                 "Use --speaker NAME (add --default to remember it).")
    sp = find_speaker(name)
    if not sp:
        found = sorted(s.player_name for s in discover())
        listing = "\n  ".join(found) if found else "(none discovered)"
        sys.exit(f"Speaker '{name}' not found on the network.\n"
                 f"Speakers found:\n  {listing}")
    return sp


# --------------------------------------------------------------------------- #
# Announcements (say): snapshot, play clip, then restore playback
# --------------------------------------------------------------------------- #
def play_once(speaker, timeout=30):
    """Wait for the clip to play through once, then stop it.

    The TTS stream is delivered over the x-rincon-mp3radio:// scheme, which
    Sonos treats like internet radio: when the short clip ends it reconnects
    and replays it. We watch for the first PLAYING -> (TRANSITIONING/STOPPED)
    transition and call stop() so it plays exactly once.
    """
    start = time.time()
    played = False
    while time.time() - start < timeout:
        state = speaker.get_current_transport_info()["current_transport_state"]
        if state == "PLAYING":
            played = True
        elif played and state in ("TRANSITIONING", "STOPPED", "PAUSED_PLAYBACK"):
            break
        time.sleep(0.2)
    speaker.stop()


def announce(speaker, uri, volume=None):
    snap = Snapshot(speaker)
    snap.snapshot()
    try:
        if volume is not None:
            speaker.volume = max(0, min(100, volume))
        speaker.play_uri(uri)
        play_once(speaker)
    finally:
        snap.restore(fade=True)


def broadcast(text, volume=None):
    speakers = discover()
    if not speakers:
        print("No Sonos speakers found.")
        return
    uri = tts_uri(text)
    threads = [threading.Thread(target=announce, args=(sp, uri, volume))
               for sp in speakers]
    for t in threads:   # start together so they speak simultaneously
        t.start()
    for t in threads:
        t.join()
    names = ", ".join(sorted(s.player_name for s in speakers))
    print(f"Broadcast to {len(speakers)} speaker(s): {names}")


def tts_uri(text, lang="en"):
    q = urllib.parse.quote(text)
    # Use the x-rincon-mp3radio:// scheme so Sonos treats the TTS stream as
    # internet radio. The plain https:// URL makes play_uri() fail with
    # "UPnP Error 714 Illegal MIME-Type" on many Sonos models.
    return ("x-rincon-mp3radio://translate.google.com/translate_tts"
            f"?ie=UTF-8&q={q}&tl={lang}&client=tw-ob")


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #
def _safe_name(s):
    try:
        return s.player_name
    except Exception:
        return ""


def print_speakers(speakers):
    """Print speakers sorted by name; return the list actually shown."""
    rows = sorted(speakers, key=_safe_name)
    if not rows:
        print("No Sonos speakers found.")
        return rows
    for s in rows:
        try:
            print(f"{s.player_name:25} {s.ip_address}")
        except Exception as e:
            # Don't let one unreachable/misbehaving speaker abort the listing.
            print(f"{'<unavailable>':25} {getattr(s, 'ip_address', '?')}  ({e})")
    return rows


def _parse_didl_fields(meta):
    """Pull every populated tag out of a DIDL metadata blob (namespaces
    stripped), e.g. originalTrackNumber, genre, streamContent, albumArtURI."""
    import xml.etree.ElementTree as ET
    fields = {}
    if not meta:
        return fields
    try:
        root = ET.fromstring(meta)
    except Exception:
        return fields
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        text = (el.text or "").strip()
        if text and tag not in ("DIDL-Lite", "item"):
            fields.setdefault(tag, text)
    return fields


def show_status(sp):
    info = sp.get_current_transport_info()
    track = sp.get_current_track_info()
    print(f"Speaker:    {sp.player_name}  ({sp.ip_address})")
    print(f"State:      {info.get('current_transport_state', '')}")
    status = info.get("current_transport_status")
    if status and status != "OK":
        print(f"Status:     {status}")

    # Look up Apple Music catalogue details up front (HLS now-playing metadata
    # is often sparse) so we can also fill in a missing track duration.
    d, cat_len = None, ""
    m = re.search(r"song(?:%3[aA]|:)(\d+)", track.get("uri", ""))
    if m:
        try:
            d = _itunes_lookup_track(m.group(1))
        except Exception:
            d = None
        if d and d.get("trackTimeMillis"):
            ms = d["trackTimeMillis"]
            cat_len = f"{ms // 60000}:{ms // 1000 % 60:02d}"

    # Everything get_current_track_info() exposes.
    for label, key in (("Title", "title"), ("Artist", "artist"),
                       ("Album", "album")):
        if track.get(key):
            print(f"{label + ':':12}{track[key]}")
    pos, dur = track.get("position", ""), track.get("duration", "")
    if dur in ("", "0:00:00") and cat_len:   # Sonos didn't report it; use catalogue
        dur = cat_len
    if pos or dur:
        print(f"{'Position:':12}{pos} / {dur}")
    if track.get("playlist_position"):
        print(f"{'Queue pos:':12}{track['playlist_position']}")
    if track.get("album_art"):
        print(f"{'Album art:':12}{track['album_art']}")
    if track.get("uri"):
        print(f"{'URI:':12}{track['uri']}")

    if d:
        print("\nApple Music catalogue:")
        for label, val in (
            ("Title", d.get("trackName")),
            ("Artist", d.get("artistName")),
            ("Album", d.get("collectionName")),
            ("Genre", d.get("primaryGenreName")),
            ("Released", (d.get("releaseDate") or "")[:10]),
            ("Length", cat_len),
            ("Track", f"{d.get('trackNumber', '')}/{d.get('trackCount', '')}"),
            ("Disc", f"{d.get('discNumber', '')}/{d.get('discCount', '')}"),
            ("Explicit", d.get("trackExplicitness")),
            ("Country", d.get("country")),
        ):
            if val and str(val).strip(" /"):
                print(f"  {label + ':':11}{val}")

    # Any extra tags carried in the track metadata that we didn't print above.
    shown = {"title", "creator", "artist", "album", "albumArtURI", "class", "res"}
    for tag, val in _parse_didl_fields(track.get("metadata", "")).items():
        if tag not in shown:
            print(f"{tag + ':':12}{val}")

    # Player / playback settings.
    for label, getter in (
        ("Volume", lambda: f"{sp.volume}{'  (muted)' if sp.mute else ''}"),
        ("Play mode", lambda: sp.play_mode),
        ("Cross-fade", lambda: sp.cross_fade),
        ("Tone", lambda: f"bass {sp.bass}, treble {sp.treble}, "
                         f"loudness {sp.loudness}"),
    ):
        try:
            print(f"{label + ':':12}{getter()}")
        except Exception:
            pass


def absolute_volume(spec):
    """Return an int 0-100 if spec is an absolute volume, else None
    (relative +N/-N, the show sentinel, or junk)."""
    if not spec or spec == VOL_SHOW:
        return None
    s = spec.strip()
    if s[:1] in "+-":
        return None
    try:
        return max(0, min(100, int(s)))
    except ValueError:
        return None


def apply_volume(sp, spec):
    """Set sp's volume from an absolute (e.g. 40) or relative (e.g. +5/-5) spec."""
    s = spec.strip()
    try:
        if s[:1] in "+-":
            sp.volume = max(0, min(100, sp.volume + int(s)))
        else:
            sp.volume = max(0, min(100, int(s)))
    except ValueError:
        sys.exit(f"Invalid volume '{spec}'. Use 0-100, or +N / -N (e.g. --volume=-5).")
    return sp.volume


def show_volumes_all():
    """Print the current volume of every speaker."""
    speakers = discover()
    if not speakers:
        print("No Sonos speakers found.")
        return
    for s in sorted(speakers, key=_safe_name):
        try:
            print(f"{s.player_name:25} {s.volume:>3}")
        except Exception as e:
            print(f"{_safe_name(s) or '<unavailable>':25}  (error: {e})")


def print_groups(speakers):
    """List the multi-speaker groups currently set up."""
    groups = {}
    for s in speakers:
        try:
            g = s.group
        except Exception:
            continue
        if g is not None:
            groups[g.uid] = g
    multi = [g for g in groups.values() if len(g.members) > 1]
    print("\nGroups:")
    if not multi:
        print("  (none — all speakers are standalone)")
        return
    for g in multi:
        try:
            coord = g.coordinator.player_name
            others = sorted(_safe_name(m) for m in g.members
                            if m is not g.coordinator)
            print(f"  {coord} + {', '.join(others)}")
        except Exception as e:
            print(f"  (group error: {e})")


def search_library(speaker, term, stype=None):
    """Find the first match for term in the local library. stype (album/song/
    artist/playlist/genre) restricts the search; None tries them all.
    Returns (kind, item) or (None, None)."""
    ml = speaker.music_library
    target = term.lower()
    want = {"song": "track", "track": "track", "album": "album",
            "artist": "artist", "playlist": "playlist",
            "genre": "genre"}.get(stype)
    # Sonos playlists (saved in the Sonos app) — match on title.
    if stype in (None, "playlist"):
        try:
            for pl in speaker.get_sonos_playlists():
                if target in (pl.title or "").lower():
                    return "playlist", pl
        except Exception:
            pass
    cats = [("playlist", "playlists"), ("track", "tracks"),
            ("album", "albums"), ("artist", "artists"), ("genre", "genres")]
    if want:
        cats = [(l, k) for (l, k) in cats if l == want]
    for label, kind in cats:
        try:
            results = ml.get_music_library_information(
                kind, search_term=term, complete_result=True)
        except Exception:
            continue
        if results:
            return label, results[0]
    return None, None


def enqueue(speaker, ml, item):
    """Add a track or container (album/artist/genre) to the end of the queue."""
    try:
        speaker.add_to_queue(item)
    except Exception:
        # Container that can't be queued directly: add its tracks instead.
        for child in ml.browse(item):
            try:
                speaker.add_to_queue(child)
            except Exception:
                pass


def list_sources():
    """List the music sources usable with --play / --queue."""
    try:
        from soco.music_services import MusicService
        names = set(MusicService.get_subscribed_services_names())
    except Exception:
        names = set()
    # Apple Music works via the iTunes catalogue + the attached Sonos service,
    # even when soco's music-service API reports nothing — so always offer it.
    names.add("Apple Music")
    print("Music sources for --play / --queue:")
    print("  Library                  (local music library — the default)")
    for n in sorted(names):
        note = "  (albums full; single tracks = 30s preview)" \
            if n == "Apple Music" else ""
        print(f"  {n}{note}")


def is_local_source(source):
    """True if source means the local music library (None or 'library')."""
    return not source or source.strip().lower() == "library"


def search_service(service, term):
    """Search a streaming service across its categories. Returns (cat, item)."""
    available = getattr(service, "available_search_categories", []) or []
    for cat in ("playlists", "tracks", "albums", "artists", "stations", "genres"):
        if available and cat not in available:
            continue
        try:
            results = service.search(category=cat, term=term)
        except Exception:
            continue
        if results:
            return cat, results[0]
    return None, None


APPLE_MUSIC_SID = 204  # Apple Music's Sonos service ID (stable)


def _apple_cfg():
    """Apple Music playback parameters, with config.json "apple" overrides.
    sn (account serial) and flags were captured from a real queue item on this
    household; no auth token is needed (network access = authorised)."""
    cfg = load_config().get("apple") or {}
    return {
        "sn": cfg.get("sn", 6),
        "flags": cfg.get("flags", 8232),
    }


def _xesc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


def _itunes_search(term, entity, limit=5):
    """Search the public iTunes catalogue (same as Apple Music, no auth)."""
    import urllib.request
    params = urllib.parse.urlencode(
        {"term": term, "media": "music", "entity": entity, "limit": limit})
    url = f"https://itunes.apple.com/search?{params}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read()).get("results", [])


def _itunes_album_tracks(collection_id, limit=300):
    """Look up an album's tracks (in track order) by its iTunes collection id."""
    import urllib.request
    params = urllib.parse.urlencode(
        {"id": collection_id, "entity": "song", "limit": limit})
    url = f"https://itunes.apple.com/lookup?{params}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        results = json.loads(resp.read()).get("results", [])
    return [r for r in results
            if r.get("wrapperType") == "track" and r.get("trackId")]


def _itunes_lookup_track(track_id):
    """Fetch full catalogue details for a single track by its iTunes id."""
    import urllib.request
    url = f"https://itunes.apple.com/lookup?{urllib.parse.urlencode({'id': track_id})}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        results = json.loads(resp.read()).get("results", [])
    return results[0] if results else None


def _apple_track_uri(track_id, ac):
    # Matches the queue URI Sonos itself uses: HLS static + the account sn.
    return (f"x-sonosapi-hls-static:song%3a{track_id}"
            f"?sid={APPLE_MUSIC_SID}&flags={ac['flags']}&sn={ac['sn']}")


def _apple_track_meta(uri, title, artist, album):
    """Track metadata mirroring a real Apple Music queue item — note there is
    NO cdudn/desc: the sn in the URI identifies the linked account."""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="-1" parentID="-1" restricted="true">'
        f'<res protocolInfo="sonos.com-http:*:application/x-mpegURL:*">'
        f'{_xesc(uri)}</res>'
        f'<dc:title>{_xesc(title)}</dc:title>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<dc:creator>{_xesc(artist)}</dc:creator>'
        f'<upnp:album>{_xesc(album)}</upnp:album>'
        '</item></DIDL-Lite>'
    )


def _enqueue_apple_track(speaker, track, ac):
    """Add one iTunes track (dict) to the queue using the Sonos HLS URI."""
    uri = _apple_track_uri(track["trackId"], ac)
    meta = _apple_track_meta(uri, track.get("trackName", ""),
                             track.get("artistName", ""),
                             track.get("collectionName", ""))
    speaker.avTransport.AddURIToQueue([
        ("InstanceID", 0),
        ("EnqueuedURI", uri),
        ("EnqueuedURIMetaData", meta),
        ("DesiredFirstTrackNumberEnqueued", 0),
        ("EnqueueAsNext", 0),
    ])


def _apple_play_song(speaker, term, enqueue_only):
    ac = _apple_cfg()
    try:
        songs = _itunes_search(term, "song")
    except Exception as e:
        print(f"iTunes search failed: {e}")
        return None
    for s in songs:
        if not s.get("trackId"):
            continue
        title = s.get("trackName", term)
        try:
            if not enqueue_only:
                speaker.clear_queue()
            _enqueue_apple_track(speaker, s, ac)
            if not enqueue_only:
                speaker.play_from_queue(0)
            return "song", title
        except Exception as e:
            print(f"Could not play song '{title}' from Apple Music: {e}")
            return None
    return None


def _apple_play_album(speaker, term, enqueue_only):
    ac = _apple_cfg()
    try:
        albums = _itunes_search(term, "album")
    except Exception as e:
        print(f"iTunes search failed: {e}")
        return None
    for a in albums:
        cid = a.get("collectionId")
        if not cid:
            continue
        title = a.get("collectionName", term)
        try:
            tracks = _itunes_album_tracks(cid)
        except Exception as e:
            print(f"iTunes album lookup failed: {e}")
            return None
        if not tracks:
            continue
        try:
            if not enqueue_only:
                speaker.clear_queue()
            for t in tracks:
                _enqueue_apple_track(speaker, t, ac)
            if not enqueue_only:
                speaker.play_from_queue(0)
            return "album", f"{title} ({len(tracks)} tracks)"
        except Exception as e:
            print(f"Could not play album '{title}' from Apple Music: {e}")
            return None
    return None


def apple_music_play(speaker, term, enqueue_only, stype=None):
    """Search Apple Music (via iTunes) and play/queue on the speaker.
    stype restricts the kind; None tries album then song.
    Returns (kind, title) or (None, None)."""
    if stype in ("station", "artist", "playlist"):
        print(f"'{stype}' isn't supported for Apple Music yet — album and song "
              "only. (Playlists work from your local/Sonos playlists.)")
        return None, None
    order = {"album": [_apple_play_album],
             "song": [_apple_play_song]}.get(stype,
                                             [_apple_play_album, _apple_play_song])
    for fn in order:
        result = fn(speaker, term, enqueue_only)
        if result:
            return result
    return None, None


def find_and_play(speaker, term, source, enqueue_only=False, stype=None):
    """Find term in the chosen source and play or enqueue the first match.
    Returns (kind, title) or (None, None)."""
    if is_local_source(source):
        ml = speaker.music_library
        kind, item = search_library(speaker, term, stype)
        if not kind:
            return None, None
        if not enqueue_only:
            speaker.clear_queue()
        enqueue(speaker, ml, item)
        if not enqueue_only:
            speaker.play_from_queue(0)
        return kind, getattr(item, "title", term)

    # Apple Music via the iTunes catalogue + the attached Sonos service.
    if "apple" in source.lower():
        return apple_music_play(speaker, term, enqueue_only, stype)

    # Other streaming services: best-effort via soco's music-service API.
    try:
        from soco.music_services import MusicService
        service = MusicService(source)
    except Exception as e:
        print(f"Could not open music service '{source}': {e}")
        return None, None
    kind, item = search_service(service, term)
    if not kind:
        return None, None
    if not enqueue_only:
        speaker.clear_queue()
    try:
        speaker.add_to_queue(item)
        if not enqueue_only:
            speaker.play_from_queue(0)
    except Exception:
        # Fall back to playing the item's URI directly.
        try:
            speaker.play_uri(getattr(item, "uri", ""), getattr(item, "metadata", ""))
        except Exception as e:
            print(f"Found '{getattr(item, 'title', term)}' on {source}, "
                  f"but could not play it: {e}")
            return None, None
    return kind, getattr(item, "title", term)


def _candidate(kind, title, artist, enact):
    return {"kind": kind, "title": title, "artist": artist, "enact": enact}


def _apple_candidates(speaker, term, stype):
    """All Apple Music matches for term, each with an enact(enqueue_only) fn."""
    if stype in ("station", "artist", "playlist"):
        print(f"'{stype}' isn't supported for Apple Music — album and song only.")
        return []
    ac = _apple_cfg()
    kinds = {"album": ["album"], "song": ["song"]}.get(stype, ["album", "song"])
    cands = []
    if "album" in kinds:
        for a in _itunes_search(term, "album", limit=25):
            cid = a.get("collectionId")
            if not cid:
                continue
            title, artist = a.get("collectionName", ""), a.get("artistName", "")

            def enact(enqueue_only, cid=cid, title=title):
                tracks = _itunes_album_tracks(cid)
                if not tracks:
                    return None
                if not enqueue_only:
                    speaker.clear_queue()
                for t in tracks:
                    _enqueue_apple_track(speaker, t, ac)
                if not enqueue_only:
                    speaker.play_from_queue(0)
                return "album", f"{title} ({len(tracks)} tracks)"
            cands.append(_candidate("album", title, artist, enact))
    if "song" in kinds:
        for s in _itunes_search(term, "song", limit=25):
            if not s.get("trackId"):
                continue
            title, artist = s.get("trackName", ""), s.get("artistName", "")

            def enact(enqueue_only, s=s, title=title):
                if not enqueue_only:
                    speaker.clear_queue()
                _enqueue_apple_track(speaker, s, ac)
                if not enqueue_only:
                    speaker.play_from_queue(0)
                return "song", title
            cands.append(_candidate("song", title, artist, enact))
    return cands


def _local_candidates(speaker, term, stype):
    """All local-library matches for term, each with an enact(enqueue_only) fn."""
    ml = speaker.music_library
    want = {"song": "track", "track": "track", "album": "album",
            "artist": "artist", "playlist": "playlist",
            "genre": "genre"}.get(stype)
    cands = []
    if stype in (None, "playlist"):
        try:
            for pl in speaker.get_sonos_playlists():
                if term.lower() in (pl.title or "").lower():
                    def enact(enqueue_only, pl=pl):
                        if not enqueue_only:
                            speaker.clear_queue()
                        speaker.add_to_queue(pl)
                        if not enqueue_only:
                            speaker.play_from_queue(0)
                        return "playlist", pl.title
                    cands.append(_candidate("playlist", pl.title, "", enact))
        except Exception:
            pass
    cats = [("playlist", "playlists"), ("track", "tracks"), ("album", "albums"),
            ("artist", "artists"), ("genre", "genres")]
    if want:
        cats = [(l, k) for (l, k) in cats if l == want]
    for label, kind in cats:
        try:
            results = ml.get_music_library_information(
                kind, search_term=term, complete_result=True)
        except Exception:
            continue
        for item in results:
            title = getattr(item, "title", str(item))
            artist = getattr(item, "creator", "") or getattr(item, "artist", "") or ""

            def enact(enqueue_only, item=item, label=label):
                if not enqueue_only:
                    speaker.clear_queue()
                enqueue(speaker, ml, item)
                if not enqueue_only:
                    speaker.play_from_queue(0)
                return label, getattr(item, "title", term)
            cands.append(_candidate(label, title, artist, enact))
    return cands


def do_query(speaker, term, source, stype):
    """List every match for term and let the user pick one by number to play
    (or append 'q' to that number to add it to the queue instead)."""
    if is_local_source(source):
        cands, where = _local_candidates(speaker, term, stype), "Library"
    elif "apple" in (source or "").lower():
        cands, where = _apple_candidates(speaker, term, stype), source
    else:
        print("--query supports Library and Apple Music sources only.")
        return
    if not cands:
        print(f"No matches on {where} for '{term}'.")
        return
    print(f"Matches on {where} for '{term}':")
    for i, c in enumerate(cands, 1):
        sub = f" — {c['artist']}" if c["artist"] else ""
        print(f"  {i:>2}. [{c['kind']}] {c['title']}{sub}")
    try:
        choice = input("\nEnter a number to PLAY it, or add 'q' to the number to "
                       "QUEUE it (e.g. 3q). Blank to cancel: ").strip()
    except EOFError:
        return
    if not choice:
        return
    enqueue_only = choice[-1:].lower() == "q"
    num = choice[:-1].strip() if enqueue_only else choice
    if not num.isdigit() or not (1 <= int(num) <= len(cands)):
        print(f"Invalid selection '{choice}'.")
        return
    try:
        result = cands[int(num) - 1]["enact"](enqueue_only)
    except Exception as e:
        print(f"Could not play that selection: {e}")
        return
    if result:
        kind, title = result
        print(f"{'Queued' if enqueue_only else 'Playing'} {kind}: {title}")
    else:
        print("Could not play that selection.")


def _enqueue_saved(speaker, entry):
    """Add one saved entry (by its stored URI) to the queue. Returns True/False."""
    uri = entry.get("uri", "")
    if not uri:
        return False
    if "song%3a" in uri or "sid=204" in uri or uri.startswith("x-sonosapi"):
        meta = _apple_track_meta(uri, entry.get("title", ""),
                                 entry.get("artist", ""), entry.get("album", ""))
        _add_to_queue_with_meta(speaker, uri, meta)
    else:
        speaker.add_uri_to_queue(uri)
    return True


def play_saved_entry(speaker, entry, enqueue_only):
    try:
        if not enqueue_only:
            speaker.clear_queue()
        if not _enqueue_saved(speaker, entry):
            return None
        if not enqueue_only:
            speaker.play_from_queue(0)
        return entry.get("title") or entry.get("uri")
    except Exception as e:
        print(f"Could not play saved track: {e}")
        return None


def play_saved_list(speaker, entries, enqueue_only):
    """Enqueue a whole list of saved entries; returns how many were added."""
    if not enqueue_only:
        speaker.clear_queue()
    n = 0
    for e in entries:
        try:
            if _enqueue_saved(speaker, e):
                n += 1
        except Exception:
            pass
    if not enqueue_only and n:
        speaker.play_from_queue(0)
    return n


def _pick(prompt_label, rows):
    """Print numbered rows and read a selection; returns (index, queue?) or None.
    Appending 'q' to the number means queue instead of play."""
    for i, row in enumerate(rows, 1):
        print(f"  {i:>2}. {row}")
    try:
        choice = input(f"\nEnter a number to PLAY {prompt_label}, or add 'q' to "
                       "QUEUE it (e.g. 3q). Blank to cancel: ").strip()
    except EOFError:
        return None
    if not choice:
        return None
    enqueue_only = choice[-1:].lower() == "q"
    num = choice[:-1].strip() if enqueue_only else choice
    if not num.isdigit() or not (1 <= int(num) <= len(rows)):
        print(f"Invalid selection '{choice}'.")
        return None
    return int(num) - 1, enqueue_only


def do_saved(args, cfg):
    """List saved tracks and play/queue the chosen one."""
    items = load_saved_data().get("saved", [])
    if not items:
        print("No saved tracks yet. Use --save while something is playing.")
        return
    print("Saved tracks:")
    rows = []
    for e in items:
        sub = f" — {e.get('artist')}" if e.get("artist") else ""
        alb = f"  [{e.get('album')}]" if e.get("album") else ""
        rows.append(f"{e.get('title', '(unknown)')}{sub}{alb}")
    sel = _pick("it", rows)
    if not sel:
        return
    idx, enqueue_only = sel
    speaker = resolve_speaker(args, cfg)
    title = play_saved_entry(speaker, items[idx], enqueue_only)
    if title:
        verb = "Queued" if enqueue_only else "Playing"
        print(f"{verb}: {title}  on {speaker.player_name}")


def make_playlist(name):
    """Snapshot the current saved tracks into a named local playlist."""
    data = load_saved_data()
    saved = data.get("saved", [])
    if not saved:
        print("No saved tracks to make a playlist from. Use --save first.")
        return
    pls = data.setdefault("playlists", {})
    pls[name] = [dict(e) for e in saved]
    write_saved_data(data)
    print(f"Created local playlist '{name}' with {len(saved)} track(s).")
    print(f"  file: {SAVE_FILE}")


def do_playlists(args, cfg, name=None):
    """List local playlists and play one, or play a named one directly."""
    pls = load_saved_data().get("playlists", {})
    if not pls:
        print("No local playlists yet. Create one with --makeplaylist NAME.")
        return
    if name:
        match = next((k for k in pls if _normalize(k) == _normalize(name)), None)
        if not match:
            print(f"No local playlist '{name}'.")
            return
        speaker = resolve_speaker(args, cfg)
        n = play_saved_list(speaker, pls[match], enqueue_only=False)
        print(f"Playing playlist '{match}' ({n} tracks) on {speaker.player_name}.")
        return
    print("Local playlists:")
    names = sorted(pls.keys(), key=lambda s: s.lower())
    rows = [f"{nm}  ({len(pls[nm])} tracks)" for nm in names]
    sel = _pick("all its tracks", rows)
    if not sel:
        return
    idx, enqueue_only = sel
    nm = names[idx]
    speaker = resolve_speaker(args, cfg)
    n = play_saved_list(speaker, pls[nm], enqueue_only)
    verb = "Queued" if enqueue_only else "Playing"
    print(f"{verb} playlist '{nm}' ({n} tracks) on {speaker.player_name}.")


def del_playlist(name):
    data = load_saved_data()
    pls = data.get("playlists", {})
    match = next((k for k in pls if _normalize(k) == _normalize(name)), None)
    if not match:
        print(f"No local playlist '{name}'.")
        return
    del pls[match]
    write_saved_data(data)
    print(f"Deleted local playlist '{match}'.")


def add_to_playlist(name):
    """List saved tracks, take a CSV of numbers, and add them to playlist NAME
    (creating it if it doesn't exist, appending if it does)."""
    data = load_saved_data()
    items = data.get("saved", [])
    if not items:
        print("No saved tracks. Use --save first.")
        return
    print("Saved tracks:")
    for i, e in enumerate(items, 1):
        sub = f" — {e.get('artist')}" if e.get("artist") else ""
        print(f"  {i:>2}. {e.get('title', '(unknown)')}{sub}")
    try:
        raw = input(f"\nEnter comma-separated numbers to add to playlist "
                    f"'{name}': ").strip()
    except EOFError:
        return
    if not raw:
        print("Nothing selected.")
        return
    idxs = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(items):
            idxs.append(int(part) - 1)
        elif part:
            print(f"  skipping invalid '{part}'")
    if not idxs:
        print("No valid selections.")
        return
    pls = data.setdefault("playlists", {})
    match = next((k for k in pls if _normalize(k) == _normalize(name)), None)
    created = match is None
    if created:
        pls[name] = []
        match = name
    for i in idxs:
        pls[match].append(dict(items[i]))
    write_saved_data(data)
    action = "Created" if created else "Updated"
    print(f"{action} playlist '{match}': added {len(idxs)} track(s), "
          f"now {len(pls[match])} total.")


def list_queue(speaker):
    """Print the current play queue."""
    queue = speaker.get_queue(max_items=500)
    if not queue:
        print("Queue is empty.")
        return
    for i, item in enumerate(queue, 1):
        title = getattr(item, "title", "?")
        artist = getattr(item, "creator", "") or ""
        print(f"{i:>3}. {title}" + (f" — {artist}" if artist else ""))


def dump_nowplaying(sp):
    """Print the raw transport URI + metadata Sonos is currently using.
    Play something from the real Sonos app first, then run this to capture
    the exact values (service token, URI format) needed to reproduce it."""
    print(f"Speaker: {sp.player_name}  ({sp.ip_address})")
    try:
        ti = sp.get_current_transport_info()
        print("State:", ti.get("current_transport_state"))
    except Exception as e:
        print("transport-info error:", e)
    try:
        mi = sp.avTransport.GetMediaInfo([("InstanceID", 0)])
        print("\n--- CurrentURI ---\n" + (mi.get("CurrentURI") or ""))
        print("\n--- CurrentURIMetaData ---\n" + (mi.get("CurrentURIMetaData") or ""))
    except Exception as e:
        print("media-info error:", e)
    try:
        pi = sp.avTransport.GetPositionInfo([("InstanceID", 0)])
        print("\n--- TrackURI ---\n" + (pi.get("TrackURI") or ""))
        print("\n--- TrackMetaData ---\n" + (pi.get("TrackMetaData") or ""))
    except Exception as e:
        print("position-info error:", e)


def dump_queue_raw(sp):
    """Print the raw DIDL of the current queue, which (unlike now-playing
    metadata) keeps the <desc> cdudn service token we need to reproduce."""
    print(f"Queue on {sp.player_name} ({sp.ip_address}):\n")
    try:
        res = sp.contentDirectory.Browse([
            ("ObjectID", "Q:0"),
            ("BrowseFlag", "BrowseDirectChildren"),
            ("Filter", "*"),
            ("StartingIndex", 0),
            ("RequestedCount", 3),
            ("SortCriteria", ""),
        ])
        print(res.get("Result") or "(empty queue)")
    except Exception as e:
        print("browse error:", e)


def show_status_all():
    """Now-playing dashboard for every speaker."""
    speakers = discover()
    if not speakers:
        print("No Sonos speakers found.")
        return
    for s in sorted(speakers, key=_safe_name):
        try:
            state = s.get_current_transport_info()["current_transport_state"]
            track = s.get_current_track_info()
            title = track.get("title") or "—"
            artist = track.get("artist") or ""
            now = f"{title} — {artist}" if artist else title
            print(f"{s.player_name:20} {state:16} vol {s.volume:>3}  {now}")
        except Exception as e:
            print(f"{_safe_name(s) or '<unavailable>':20} (error: {e})")


def group_with(coordinator, names):
    """Join each named speaker into the coordinator's group. Returns names joined."""
    joined = []
    for name in names:
        member = find_speaker(name)
        if not member:
            print(f"  skip: '{name}' not found")
            continue
        try:
            member.join(coordinator)
            joined.append(member.player_name)
        except Exception as e:
            print(f"  skip: '{name}' ({e})")
    return joined


def party_mode(coordinator):
    """Group every other speaker with the coordinator. Returns names joined."""
    joined = []
    for s in discover():
        if s.ip_address == coordinator.ip_address:
            continue
        try:
            s.join(coordinator)
            joined.append(s.player_name)
        except Exception as e:
            print(f"  skip: {_safe_name(s) or '?'} ({e})")
    return joined


# --------------------------------------------------------------------------- #
# Favourites
# --------------------------------------------------------------------------- #
def extract_settings(args):
    """Capture the scene options the user actually set, as a plain dict."""
    defaults = {"party": False, "ungroup": False, "clearqueue": False,
                "pause": False, "stop": False, "next": False, "prev": False,
                "status": False}
    settings = {}
    for key in SCENE_KEYS:
        val = getattr(args, key)
        if val != defaults.get(key, None):
            settings[key] = val
    return settings


def describe_settings(s):
    parts = []
    for k in SCENE_KEYS:
        if k not in s:
            continue
        v = s[k]
        if isinstance(v, bool):
            if v:
                parts.append(k)
        elif v == PLAY_RESUME:
            parts.append("play")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts) or "(nothing)"


def save_favourite(cfg, name, settings):
    """Store settings under name; reuse the number if the name already exists."""
    favs = cfg.setdefault("favourites", [])
    for f in favs:
        if _normalize(f["name"]) == _normalize(name):
            f["settings"] = settings
            save_config(cfg)
            return f["number"]
    number = max((f["number"] for f in favs), default=0) + 1
    favs.append({"number": number, "name": name, "settings": settings})
    save_config(cfg)
    return number


def find_favourite(cfg, token):
    """Look up a favourite by number (digits) or by name."""
    favs = cfg.get("favourites", [])
    token = token.strip()
    if token.isdigit():
        num = int(token)
        for f in favs:
            if f["number"] == num:
                return f
    for f in favs:
        if _normalize(f["name"]) == _normalize(token):
            return f
    return None


def prompt_favourite(cfg):
    """List favourites and ask the user to pick one. Returns a fav dict or None."""
    favs = sorted(cfg.get("favourites", []), key=lambda x: x["number"])
    if not favs:
        print("No favourites saved yet.")
        return None
    print("Saved favourites:")
    for f in favs:
        print(f"  {f['number']}. {f['name']}  [{describe_settings(f['settings'])}]")
    try:
        choice = input("\nEnter a favourite number to run (blank to cancel): ").strip()
    except EOFError:
        return None
    if not choice:
        return None
    fav = find_favourite(cfg, choice)
    if not fav:
        print(f"No favourite '{choice}'.")
    return fav


def settings_to_args(settings):
    """Build an argparse-style namespace from a saved settings dict."""
    return argparse.Namespace(
        speaker=settings.get("speaker"),
        source=settings.get("source"),
        stype=settings.get("stype"),
        play=settings.get("play"),
        queue=settings.get("queue"),
        clearqueue=settings.get("clearqueue", False),
        volume=settings.get("volume"),
        group=settings.get("group"),
        party=settings.get("party", False),
        ungroup=settings.get("ungroup", False),
        sleep=settings.get("sleep"),
        say=settings.get("say"),
        broadcast=settings.get("broadcast"),
        pause=settings.get("pause", False),
        stop=settings.get("stop", False),
        next=settings.get("next", False),
        prev=settings.get("prev", False),
        status=settings.get("status", False),
        default=False,
    )


# --------------------------------------------------------------------------- #
# Action execution
# --------------------------------------------------------------------------- #
def has_actions(args):
    return any([
        args.play is not None, args.pause, args.stop, args.next, args.prev,
        args.volume is not None, args.status, args.say,
        args.group is not None, args.party, args.ungroup,
        args.sleep is not None, args.queue is not None,
        args.clearqueue, args.broadcast, args.save is not None,
    ])


def enact(args, cfg, sp=None):
    """Carry out the requested actions (a live command or a saved favourite).
    sp may be supplied to act on a specific speaker (used by --speaker all)."""
    if args.broadcast:
        broadcast(args.broadcast, absolute_volume(args.volume))
        return

    if sp is None:
        sp = resolve_speaker(args, cfg)

    # Where to search for --play/--queue: explicit --source, else saved default.
    source = (args.source if args.source and args.source != SOURCE_LIST
              else cfg.get("default_source"))
    src_label = "Library" if is_local_source(source) else source

    if args.group:
        names = [n.strip() for n in args.group.split(",") if n.strip()]
        joined = group_with(sp, names)
        print(f"Grouped with {sp.player_name}: {', '.join(joined) or '(none)'}")
    if args.party:
        joined = party_mode(sp)
        print(f"Party mode: {len(joined)} speaker(s) now playing with "
              f"{sp.player_name}.")
    if args.ungroup:
        sp.unjoin()
        print(f"{sp.player_name} removed from its group.")
    if args.volume is not None:
        print(f"Volume: {apply_volume(sp, args.volume)}")
    if args.clearqueue:
        sp.clear_queue()
        print(f"Cleared the queue on {sp.player_name}.")
    stype = getattr(args, "stype", None)
    if args.queue == QUEUE_SHOW:
        list_queue(sp)
    elif args.queue is not None:
        kind, label = find_and_play(sp, args.queue, source,
                                    enqueue_only=True, stype=stype)
        if kind:
            print(f"Queued {kind} from {src_label}: {label}")
        else:
            print(f"Nothing found on {src_label} for '{args.queue}'.")
    if args.play is not None:
        if args.play == PLAY_RESUME:
            sp.play()
        else:
            kind, label = find_and_play(sp, args.play, source,
                                        enqueue_only=False, stype=stype)
            if kind:
                print(f"Playing {kind} from {src_label}: {label}")
            else:
                print(f"Nothing found on {src_label} for '{args.play}'.")
    if args.pause:
        sp.pause()
    if args.stop:
        sp.stop()
    if args.next:
        sp.next()
    if args.prev:
        sp.previous()
    if args.sleep is not None:
        if args.sleep <= 0:
            sp.set_sleep_timer(None)
            print(f"Sleep timer cancelled on {sp.player_name}.")
        else:
            sp.set_sleep_timer(args.sleep * 60)
            print(f"Sleep timer set: {sp.player_name} stops in {args.sleep} min.")
    if args.say:
        announce(sp, tts_uri(args.say), absolute_volume(args.volume))
    if args.save is not None:
        if args.save == SAVE_LIST:
            save_current_track(sp)
        else:
            save_current_to_playlist(sp, args.save)
    if args.status:
        show_status(sp)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(
        description="Control Sonos speakers.",
        epilog="Created by Mark Burnett — www.linkedin.com/in/markburnett — "
               "github.com/embernet/sonos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list", action="store_true", help="List speakers on the network")
    p.add_argument("--speaker", metavar="NAME", help="Target speaker by name")
    p.add_argument("--source", nargs="?", const=SOURCE_LIST, default=None,
                   metavar="NAME",
                   help="Where --play/--queue search: 'Library' (local, default) "
                        "or a streaming service like 'Apple Music'. Alone: list "
                        "available sources. Saved with --default if given")
    stype = p.add_mutually_exclusive_group()
    stype.add_argument("--album", dest="stype", action="store_const",
                       const="album", help="Restrict --play/--queue to albums")
    stype.add_argument("--artist", dest="stype", action="store_const",
                       const="artist", help="Restrict --play/--queue to artists")
    stype.add_argument("--song", "--track", dest="stype", action="store_const",
                       const="song", help="Restrict --play/--queue to songs")
    stype.add_argument("--station", dest="stype", action="store_const",
                       const="station", help="Restrict --play/--queue to stations")
    stype.add_argument("--playlist", dest="stype", action="store_const",
                       const="playlist", help="Restrict --play/--queue to playlists")
    p.set_defaults(stype=None)
    p.add_argument("--default", action="store_true",
                   help="With --speaker: remember it as the default. Alone: "
                        "show the current default speaker and its IP")
    p.add_argument("--temp", action="store_true",
                   help="With --speaker: use it as the default for the rest of "
                        "today only, then revert to the base default")
    p.add_argument("--play", nargs="?", const=PLAY_RESUME, default=None,
                   metavar="QUERY",
                   help="Resume playback; or with QUERY, play a matching song/"
                        "album/artist/genre from the Sonos music library")
    p.add_argument("--pause", action="store_true", help="Pause playback")
    p.add_argument("--stop", action="store_true", help="Stop playback")
    p.add_argument("--next", action="store_true", help="Skip to next track")
    p.add_argument("--prev", "--previous", dest="prev", action="store_true",
                   help="Go to previous track")
    p.add_argument("--volume", nargs="?", const=VOL_SHOW, default=None, metavar="N",
                   help="Set volume: absolute 0-100, or relative +N / -N "
                        "(use --volume=-5 for a decrease). Alone: show all volumes")
    p.add_argument("--status", action="store_true", help="Show now-playing info")
    p.add_argument("--status-all", action="store_true", dest="status_all",
                   help="Show now-playing for every speaker at once")
    p.add_argument("--dump", action="store_true",
                   help="Diagnostic: print the raw URI/metadata --speaker is "
                        "currently playing (capture real Apple Music values)")
    p.add_argument("--dumpqueue", action="store_true",
                   help="Diagnostic: print the raw DIDL of --speaker's queue "
                        "(shows the cdudn service token)")
    p.add_argument("--group", metavar='"A,B,..."',
                   help="Group the named speakers (comma-separated) with "
                        "--speaker so they play in sync")
    p.add_argument("--party", action="store_true",
                   help="Whole-home audio: group ALL speakers with --speaker")
    p.add_argument("--ungroup", action="store_true",
                   help="Remove --speaker from its current group")
    p.add_argument("--sleep", type=int, metavar="MIN",
                   help="Sleep timer: stop --speaker after MIN minutes (0 cancels)")
    p.add_argument("--query", metavar="NAME",
                   help="List all matches for NAME (honours --song/--album/etc.) "
                        "and pick one by number to play; add 'q' to the number "
                        "to queue it instead")
    p.add_argument("--queue", nargs="?", const=QUEUE_SHOW, default=None,
                   metavar="QUERY",
                   help="Add a song/album/artist/genre to the end of the queue; "
                        "alone (no QUERY) shows the current queue")
    p.add_argument("--clearqueue", action="store_true", help="Clear the play queue")
    p.add_argument("--save", nargs="?", const=SAVE_LIST, default=None,
                   metavar="PLAYLIST",
                   help="Save the currently-playing track to the saved list; or "
                        "with PLAYLIST, append it straight to that local playlist")
    p.add_argument("--remove", nargs="?", const=REMOVE_LAST, default=None,
                   metavar="N",
                   help="Remove a saved track: the last one, or the Nth as "
                        "numbered by --saved")
    p.add_argument("--saved", action="store_true",
                   help="List saved tracks and pick one by number to play "
                        "(add 'q' to the number to queue it)")
    p.add_argument("--makeplaylist", metavar="NAME",
                   help="Save the current saved tracks as a named local playlist")
    p.add_argument("--addtoplaylist", metavar="NAME",
                   help="List saved tracks, take a CSV of numbers, and add them "
                        "to playlist NAME (creates it if needed)")
    p.add_argument("--delplaylist", metavar="NAME",
                   help="Delete a named local playlist")
    p.add_argument("--playlists", nargs="?", const=PLAYLISTS_LIST, default=None,
                   metavar="NAME",
                   help="List local playlists and pick one to play; or with NAME, "
                        "play that playlist directly")
    p.add_argument("--say", metavar="TEXT", help="Speak text on the speaker")
    p.add_argument("--broadcast", metavar="TEXT",
                   help="Speak text on ALL speakers simultaneously")
    p.add_argument("--favourite", "--favorite", dest="favourite", nargs="?",
                   const=FAV_LIST, default=None, metavar="NAME|NUMBER",
                   help="With other options: save them as a named favourite. "
                        "With a name/number alone: run that favourite. "
                        "Alone: list favourites and pick one to run")
    p.add_argument("--seed-ip", nargs=2, metavar=("NAME", "IP"), dest="seed_ip",
                   help="Save a known speaker by NAME and IP, used when discovery "
                        "fails (needed on a Pi where multicast doesn't work). The "
                        "speaker is also added to the remembered list")
    p.add_argument("--remember", action="store_true",
                   help="With --list: live-scan the network and save the found "
                        "speakers (name + IP) to the config file")
    p.add_argument("--reset", action="store_true",
                   help="Clear all saved settings (remembered speakers, default "
                        "speaker, seed IP)")
    p.add_argument("--clear", action="store_true",
                   help="Clear only the remembered speaker list")
    args = p.parse_args()

    cfg = load_config()

    if args.reset:
        save_config({})
        print("Cleared all saved settings "
              "(remembered speakers, default speaker, seed IP).")
        return

    if args.clear:
        cfg.pop("speakers", None)
        save_config(cfg)
        print("Cleared the remembered speaker list.")
        return

    if args.remove is not None:
        remove_saved(args.remove)
        return

    if args.makeplaylist:
        make_playlist(args.makeplaylist)
        return

    if args.addtoplaylist:
        add_to_playlist(args.addtoplaylist)
        return

    if args.delplaylist:
        del_playlist(args.delplaylist)
        return

    if args.seed_ip:
        name, ip = args.seed_ip
        speakers = _speakers_from_seed(ip)
        if not speakers:
            sys.exit(f"Could not reach a Sonos speaker at {ip}.")
        cfg["seed_ip"] = ip
        # Register the named speaker in the remembered list so --speaker NAME
        # resolves to it directly, without discovery.
        spk = cfg.setdefault("speakers", [])
        for e in spk:
            if e.get("ip") == ip or _normalize(e.get("name", "")) == _normalize(name):
                e["name"], e["ip"] = name, ip
                break
        else:
            spk.append({"name": name, "ip": ip})
        save_config(cfg)
        print(f"Seed set: '{name}' at {ip}. Household speakers reachable via it:")
        for n in sorted(s.player_name for s in speakers):
            print(f"  {n}")
        return

    if args.list or args.remember:
        # --remember forces a fresh live scan; plain --list uses the saved
        # list when available (and falls back to live discovery otherwise).
        speakers = live_discover() if args.remember else discover()
        rows = print_speakers(speakers)
        print_groups(speakers)
        if args.remember:
            saved = []
            for s in rows:
                try:
                    saved.append({"name": s.player_name, "ip": s.ip_address})
                except Exception:
                    pass
            cfg["speakers"] = saved
            print(f"\nRemembered {len(saved)} speaker(s).")
            save_config(cfg)
        return

    if args.status_all:
        show_status_all()
        return

    if args.source == SOURCE_LIST:
        list_sources()
        return

    if args.volume == VOL_SHOW:
        if args.speaker:
            sp = resolve_speaker(args, cfg)
            print(f"{sp.player_name}: {sp.volume}")
        else:
            show_volumes_all()
        return

    if args.dump:
        dump_nowplaying(resolve_speaker(args, cfg))
        return

    if args.dumpqueue:
        dump_queue_raw(resolve_speaker(args, cfg))
        return

    if args.query is not None:
        source = (args.source if args.source and args.source != SOURCE_LIST
                  else cfg.get("default_source"))
        do_query(resolve_speaker(args, cfg), args.query, source, args.stype)
        return

    if args.saved:
        do_saved(args, cfg)
        return

    if args.playlists is not None:
        name = None if args.playlists == PLAYLISTS_LIST else args.playlists
        do_playlists(args, cfg, name)
        return

    if args.favourite is not None:
        if args.favourite == FAV_LIST:
            # Bare --favourite: list them and prompt for a number.
            fav = prompt_favourite(cfg)
            if fav:
                print(f"\nEnacting favourite #{fav['number']}: '{fav['name']}'")
                enact(settings_to_args(fav["settings"]), cfg)
            return
        settings = extract_settings(args)
        if any(k in settings for k in ACTION_KEYS):
            # Other options present: save them as this favourite, then run them.
            number = save_favourite(cfg, args.favourite, settings)
            print(f"Saved as favourite #{number}: '{args.favourite}'  "
                  f"[{describe_settings(settings)}]")
            enact(args, cfg)
        else:
            # Just a name/number: run the saved favourite.
            fav = find_favourite(cfg, args.favourite)
            if not fav:
                sys.exit(f"No favourite matching '{args.favourite}'. "
                         "Use --favourite (alone) to list them.")
            print(f"Enacting favourite #{fav['number']}: '{fav['name']}'")
            enact(settings_to_args(fav["settings"]), cfg)
        return

    if args.temp:
        if not args.speaker:
            # Bare --temp clears today's temporary default.
            if cfg.pop("temp_default", None):
                save_config(cfg)
                print(f"Temporary default cleared; reverted to "
                      f"'{cfg.get('default_speaker') or '(none)'}'.")
            else:
                print("No temporary default is set.")
            return
        sp = find_speaker(args.speaker)
        if not sp:
            sys.exit(f"Speaker '{args.speaker}' not found.")
        cfg["temp_default"] = {"speaker": sp.player_name,
                               "date": datetime.date.today().isoformat()}
        save_config(cfg)
        print(f"Temporary default set to '{sp.player_name}' for the rest of "
              f"today (then reverts to '{cfg.get('default_speaker') or '(none)'}').")
        # fall through so --temp can combine with other actions

    if args.default:
        has_source = args.source and args.source != SOURCE_LIST
        if args.speaker or has_source:
            if args.speaker:
                sp = find_speaker(args.speaker)
                if not sp:
                    sys.exit(f"Speaker '{args.speaker}' not found.")
                cfg["default_speaker"] = sp.player_name
                print(f"Default speaker set to '{sp.player_name}'.")
            if has_source:
                cfg["default_source"] = args.source
                print(f"Default source set to '{args.source}'.")
            save_config(cfg)
            # fall through so --default can combine with other actions
        else:
            name = cfg.get("default_speaker")
            if name:
                ip = ip_for_name(name) or "(IP unknown)"
                print(f"Default speaker: {name}  {ip}")
            else:
                print("No default speaker is set.")
            temp = cfg.get("temp_default")
            if temp and temp.get("date") == datetime.date.today().isoformat():
                print(f"Today's temporary default: {temp.get('speaker')}")
            print(f"Default source: {cfg.get('default_source') or 'Library (local)'}")
            return

    if not has_actions(args):
        if not args.default and not args.temp:
            p.print_help()
        return

    if args.speaker and args.speaker.lower() == "all":
        speakers = sorted(discover(), key=_safe_name)
        if not speakers:
            print("No speakers found.")
            return
        for sp in speakers:
            print(f"== {sp.player_name} ==")
            per = argparse.Namespace(**vars(args))
            per.speaker = sp.player_name
            try:
                enact(per, cfg, sp=sp)
            except Exception as e:
                print(f"  error: {e}")
    else:
        enact(args, cfg)


if __name__ == "__main__":
    main()

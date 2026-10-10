#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
airplay-hub: a web console for an Orange Pi AI Pro audio hub.

Drives AirPlay (shairport-sync), MPD playback (direct CD reads via
cdio_paranoia plus the local library) and CD ripping with cdparanoia -> flac.
Pure Python standard library.
"""
import base64
import json
import os
import re
import copy
import logging
from pathlib import Path
import tempfile
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

from . import cdmeta
from .http_contract import RequestError, REQUEST_TIMEOUT, read_action, validate_action
from .http_server import PlayerHTTPServer
from .mpd_client import execute as _mpd_run, quote as mpd_quote
from .storage import library_path
from .processes import run_until_quiet, terminate_and_wait
from .background import RefreshWorker
from .optical import OpticalDrive

logger = logging.getLogger(__name__)

CD = "/dev/sr0"
LIB = "/var/lib/mpd/music"          # MPD music library root
HOST, PORT = "0.0.0.0", 8080

# ---------- Audio output chain: MPD -> PulseAudio -> USB DAC / Bluetooth A2DP ----------
# PulseAudio bluez card active profile -> human-readable codec name
BT_PROFILE_NAME = {
    "a2dp-sink": "SBC", "a2dp-sink-sbc": "SBC", "a2dp-sink-sbc_xq": "SBC-XQ",
    "a2dp-sink-aac": "AAC", "a2dp-sink-aptx": "aptX",
    "a2dp-sink-aptx_hd": "aptX-HD", "a2dp-sink-aptx_ll": "aptX-LL",
    "a2dp-sink-ldac": "LDAC", "a2dp-sink-faststream": "FastStream",
}
# Typical A2DP link bitrate in kbps per codec (44.1 kHz stereo)
BT_PROFILE_KBPS = {
    "a2dp-sink": 328, "a2dp-sink-sbc": 328, "a2dp-sink-sbc_xq": 452,
    "a2dp-sink-aac": 264, "a2dp-sink-aptx": 352, "a2dp-sink-aptx_hd": 576,
    "a2dp-sink-aptx_ll": 352, "a2dp-sink-ldac": 990,
    "a2dp-sink-faststream": 264,
}
# The sink that counts as the wired output. PulseAudio and PipeWire name sinks
# after the attached hardware (alsa_output.usb-...), so this cannot be a
# constant. AIRPLAY_HUB_WIRED_SINK overrides the name for installs that define
# their own; otherwise the first non-Bluetooth sink is used.
WIRED_SINK = os.environ.get("AIRPLAY_HUB_WIRED_SINK", "").strip() or None
_link_cache = {"t": 0.0, "v": None}
_link_lock = threading.Lock()

# ---------- Bluetooth: power, discovery and pairing ----------
# The service user may reach BlueZ over D-Bus, so this process drives
# bluetoothctl directly and needs no sudoers entry.
BT_SHOW_S = 4.0          # minimum interval between full background refreshes (s)
BT_SCAN_S = 6.0          # one discovery window; NEW events cluster in the first 2-5 s
_BT_ADDR_RE = re.compile(r"([0-9A-F:]{17})")
_BT_CTRL_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_bt_state = {"powered": None, "discovering": False, "devices": [],
             "err": None, "t": 0.0}
_bt_lock = threading.Lock()
_bt_busy = {"mac": None, "act": None, "msg": "", "t": 0.0}
                                    # background pair/connect running (UI shows "Connecting...")
_bt_last = {"ok": False, "msg": "", "t": 0.0}
                                    # result banner for the most recent action (TTL ~6 s)

_toc = {"t": 0.0, "payload": None}
_toc_lock = threading.Lock()
_meta_mem = {"discid": None, "meta": None, "t": 0.0}
_rip_lock = threading.Lock()
_rip = {"running": False, "total": 0, "done": 0, "cur": None,
        "album": None, "err": None, "finished_t": 0.0}
_drive = OpticalDrive()
_q_lock = threading.RLock()          # serialises queue rebuilds; clear+add+play is atomic


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        text = p.stdout or ""
        if p.stderr:
            text += "\n" + p.stderr
        return p.returncode, text
    except subprocess.TimeoutExpired:
        return -1, "command timed out"
    except Exception as e:
        return -2, str(e)


# ---------- Disc TOC and metadata ----------

def get_toc_payload():
    now = time.monotonic()
    with _toc_lock:
        cached = copy.deepcopy(_toc["payload"])
        if cached is not None and now - _toc["t"] < 20:
            return cached
    token = _drive.claim("toc")
    if token is None:
        return cached if cached is not None else {
            "tracks": [], "err": "Optical drive busy, please wait", "offset": 0,
            "album": None, "artist": None, "year": None, "discid": None}
    try:
        # A second request may have refreshed the cache before this claim.
        with _toc_lock:
            if _toc["payload"] is not None and time.monotonic() - _toc["t"] < 20:
                return copy.deepcopy(_toc["payload"])
        if _cd_playback_active():
            return cached if cached is not None else {
                "tracks": [], "err": "CD playback is active; stop it before reading the disc", "offset": 0,
                "album": None, "artist": None, "year": None, "discid": None}
        return _read_toc_payload()
    finally:
        _drive.release(token)


def _read_toc_payload():
    """Caller must own the optical drive; slow I/O never holds the cache lock."""
    now = time.monotonic()
    rc, out = run(["cdparanoia", "-d", CD, "-Q"], timeout=25)
    tracks, err = [], None
    if rc != 0:
        err = "The optical drive is not responding or the tray is empty"
    else:
        pat = re.compile(r"^\s*(\d+)\.\s+\d+\s+\[(\d+):(\d+)\.(\d+)\]")
        for line in out.splitlines():
            m = pat.match(line)
            if m:
                mm, ss, ff = int(m.group(2)), int(m.group(3)), int(m.group(4))
                tracks.append({"n": int(m.group(1)),
                               "dur": round(mm * 60 + ss + ff / 75.0, 1)})
        if not tracks:
            err = "The disc contains no playable audio tracks"
    payload = {"tracks": tracks, "err": err, "offset": 0,
               "album": None, "artist": None, "year": None, "discid": None}
    if not err:
        discid, fields, iderr = cdmeta.disc_id()
        if iderr:
            payload["err"] = iderr
        else:
            payload["discid"] = discid
            meta, metaerr = None, None
            if _meta_mem["discid"] == discid and now - _meta_mem["t"] < 3600:
                meta, metaerr = _meta_mem["meta"], None
            else:
                meta, metaerr = cdmeta.resolve(discid, fields)
                _meta_mem.update(discid=discid, meta=meta, t=time.monotonic())
            if meta:
                payload["album"] = meta.get("album")
                payload["artist"] = meta.get("artist")
                payload["year"] = meta.get("year")
                # Offset from in-disc track numbers to album-wide numbering for merged multi-disc sets
                try:
                    payload["offset"] = int(meta.get("offset") or 0)
                except (TypeError, ValueError):
                    payload["offset"] = 0
                tl = meta.get("tracks") or []
                for i, t in enumerate(tracks):
                    if i < len(tl):
                        t["title"] = tl[i]
            elif metaerr:
                payload["err"] = metaerr
    with _toc_lock:
        _toc.update(t=time.monotonic(), payload=copy.deepcopy(payload))
    return payload


# ---------- MPD control ----------

def mpc(*args):
    return run(["mpc"] + list(args))


def _cd_playback_active():
    # A local track in a mixed queue can advance to CD without another HTTP action.
    rc, playlist = mpc("playlist", "-f", "%file%")
    if rc:
        return True  # Unknown MPD state must not be treated as an idle drive.
    if not any(line.startswith("cdda://") for line in playlist.splitlines()):
        return False
    rc, status = mpc("status")
    return rc != 0 or any(value in status for value in ("[playing]", "[paused]"))


# ---------- AirPlay integration: the shairport-sync metadata pipe ----------
# shairport-sync metadata pipe: one XML element per line
#   <item><type>hex</type><code>hex</code><length>N</length>
#   <data encoding="base64">
#   <b64 payload>
#   </data></item>
# type=ssnc carries session events (pbeg/pend/pfls/prsm), prgr progress as
# "<start>/<now>/<end>" RTP frames at 44100 fps, and PICT cover art (already
# decoded bytes). type=core carries minm/asar/asal track information.
AP_FIFO = "/tmp/shairport-sync-metadata"
_AP_RTP = 44100.0
_AP_MAX_PAYLOAD = 8 * 1024 * 1024
_AP_MAX_LINE = 4096

# AirPlay progress compensation, in seconds.
#
#
#
# The "cur" field of a prgr packet is not computed on this device: the sender
# passes it in via SET_PARAMETER "progress: <start>/<now>/<end>" (rtsp.c:1130).
# It therefore shares the sending player's timeline instead of a locally derived one.
#
# The moment shairport hands a frame to the backend is (player.c:1305):
#       release = packet_time + (latency + latency_offset - backend_buffer_length)
# and the frame then spends backend_buffer_length travelling through the backend,
#       moment sound emerges = packet_time + latency + latency_offset
#   => backend_buffer_length cancels out, so changing it cannot move the moment
#      sound emerges; only audio_backend_latency_offset_in_seconds can.
#
#   How far ahead of the audio a reported prgr position runs is therefore set by
#   the latency offset in /etc/shairport-sync.conf. With the usual -0.40 s offset
#   the residual compensation is about 0.05 s. Override it with the
#   AIRPLAY_HUB_AP_LATENCY environment variable.
try:
    _AP_LAT = float(os.environ.get("AIRPLAY_HUB_AP_LATENCY", "0.05"))
except ValueError:
    _AP_LAT = 0.05
_AP_HDR_RE = re.compile(
    r"<item><type>([0-9a-fA-F]+)</type><code>([0-9a-fA-F]+)</code>"
    r"<length>(\d+)</length>")
_ap = {"active": False, "paused": False, "title": "", "artist": "", "album": "",
       "elapsed": 0.0, "duration": 0.0, "art_t": 0.0,
       "elapsed_at": 0.0}
_ap_art = {"data": b"", "mime": ""}

# shairport also writes cover art to a cache directory; used when the pipe has no PICT
_AP_ART_CACHE_DIR = "/tmp/shairport-sync/.cache/coverart"
_ap_cache_memo = {"p": "", "mt": 0.0, "data": b""}


def _ap_cache_art():
    """Return (data, mime, mtime) of the newest cached cover, or ("", "", 0.0)."""
    try:
        d = _AP_ART_CACHE_DIR
        ps = [os.path.join(d, x) for x in os.listdir(d)]
        ps = [x for x in ps if os.path.isfile(x)]
        if not ps:
            return b"", "", 0.0
        p = max(ps, key=os.path.getmtime)
        mt = os.path.getmtime(p)
        if _ap_cache_memo["p"] != p or _ap_cache_memo["mt"] != mt:
            with open(p, "rb") as fh:
                data = fh.read(_AP_MAX_PAYLOAD + 1)
            if len(data) > _AP_MAX_PAYLOAD:
                return b"", "", 0.0
            _ap_cache_memo.update(p=p, mt=mt, data=data)
        else:
            data = _ap_cache_memo["data"]
        mime = ("image/png" if data[:4] == b"\x89PNG" else "image/jpeg")
        return data, mime, mt
    except Exception:
        return b"", "", 0.0
_ap_lock = threading.Lock()


def _ap_t4(hexstr):
    """Decode an 8-digit hex code into four characters ('73736e63' -> 'ssnc')."""
    try:
        return struct.pack(">I", int(hexstr, 16)).decode("latin-1")
    except (ValueError, struct.error):
        return ""


def _ap_dispatch(t, c, payload):
    with _ap_lock:
        _ap["updated_at"] = time.time()   # any packet is evidence that the session is alive
    if t == "ssnc":
        if c == "pbeg":
            with _ap_lock:
                _ap.update(active=True, paused=False, elapsed=0.0,
                           duration=0.0, elapsed_at=time.time())
        elif c == "pend":
            # only pend really ends the session
            with _ap_lock:
                _ap.update(active=False, paused=False, elapsed=0.0,
                           duration=0.0)
        elif c == "pfls":
            # pfls is pause/flush, not the end of the session. Freeze progress and never
            # extrapolate while paused, or resuming would over-count the paused time.
            with _ap_lock:
                if _ap.get("active"):
                    el = (_ap.get("elapsed", 0.0)
                          + max(0.0, time.time() - _ap.get("elapsed_at", 0.0)))
                    dur = _ap.get("duration", 0.0)
                    if dur > 0:
                        el = min(el, dur)
                    _ap.update(paused=True, elapsed=el,
                               elapsed_at=time.time())
                else:
                    _ap["paused"] = True
        elif c == "prsm":
            with _ap_lock:
                _ap.update(active=True, paused=False, elapsed_at=time.time())
        elif c == "prgr":
            p = payload.decode("latin-1", "replace").split("/")
            if len(p) == 3:
                try:
                    start, cur, end = (int(x) for x in p)
                    with _ap_lock:
                        # subtract the output-path latency so progress matches what is actually heard
                        _ap["elapsed"] = max(0.0, (cur - start) / _AP_RTP - _AP_LAT)
                        _ap["duration"] = max(0.0, (end - start) / _AP_RTP)
                        _ap["elapsed_at"] = time.time()
                except ValueError:
                    pass
        elif c == "PICT":
            mime = ("image/png" if payload[:4] == b"\x89PNG"
                    else "image/jpeg")
            with _ap_lock:
                _ap_art["data"] = payload
                _ap_art["mime"] = mime
                _ap["art_t"] = time.time()
    elif t == "core":
        key = {"minm": "title", "asar": "artist", "asal": "album"}.get(c)
        if key:
            with _ap_lock:
                _ap[key] = payload.decode("utf-8", "replace").strip()


def _ap_read_b64(f, nchars):
    """Read exactly nchars base64 characters, skipping in-stream newlines.

    Only the remaining deficit is requested from the stream at a time, so this never
    reads past the end of the current packet's base64 block."""
    if nchars < 0 or nchars > 4 * ((_AP_MAX_PAYLOAD + 2) // 3):
        return None
    buf = bytearray()
    wire_bytes = 0
    while len(buf) < nchars:
        chunk = f.read(min(65536, nchars - len(buf)))
        if not chunk:
            return None
        wire_bytes += len(chunk)
        if wire_bytes > nchars + max(4096, nchars // 4):
            return None
        if b"\n" in chunk or b"\r" in chunk:
            chunk = chunk.replace(b"\n", b"").replace(b"\r", b"")
        buf.extend(chunk)
    return buf


def _ap_pump(f):
    while True:
        line = f.readline(_AP_MAX_LINE + 1)
        if not line or len(line) > _AP_MAX_LINE:
            return                       # writer closed (service restart); reopen shortly
        m = _AP_HDR_RE.search(line.decode("latin-1", "replace"))
        if not m:
            continue
        t, c, ln = _ap_t4(m.group(1)), _ap_t4(m.group(2)), int(m.group(3))
        if ln > _AP_MAX_PAYLOAD:
            logger.warning("AirPlay metadata exceeds payload limit")
            return
        payload = b""
        if ln > 0:
            tag = f.readline(_AP_MAX_LINE + 1)
            if len(tag) > _AP_MAX_LINE or b"base64" not in tag:
                return
            n = 4 * ((ln + 2) // 3)
            b64 = _ap_read_b64(f, n)
            if b64 is None:
                return                   # stream out of sync or EOF; reopen
            try:
                payload = base64.b64decode(b64, validate=True)
            except Exception:
                return
            if len(payload) != ln:
                return
            if len(f.readline(_AP_MAX_LINE + 1)) > _AP_MAX_LINE:
                return
            if len(f.readline(_AP_MAX_LINE + 1)) > _AP_MAX_LINE:
                return
        _ap_dispatch(t, c, payload)


def _ap_meta_worker():
    while True:
        try:
            f = open(AP_FIFO, "rb", buffering=0)
        except OSError:
            time.sleep(2)
            continue
        try:
            _ap_pump(f)
        except Exception:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass
        time.sleep(1)


_ap_pa_probe = {"t": 0.0, "on": False}


def _ap_pa_playing():
    """PulseAudio fallback probe. A restart misses the pbeg session event, which
    would leave the metadata state machine convinced AirPlay is idle. As long as
    shairport's sink-input exists in PulseAudio the session is live. Cached for 2s."""
    now = time.time()
    if now - _ap_pa_probe["t"] < 2.0:
        return _ap_pa_probe["on"]
    on = False
    try:
        rc, out = run(["pactl", "list", "sink-inputs"])
        on = "Shairport Sync" in out
    except Exception:
        pass
    _ap_pa_probe["t"] = now
    _ap_pa_probe["on"] = on
    return on


def ap_view():
    """AirPlay snapshot for the status endpoint; elapsed extrapolates from the last
    prgr packet plus the time since it arrived.

    Staleness guard, so a restart or a parser resync cannot leave the state wedged:
    shairport 3.3.8 sends prgr only on track changes, never periodically, so
    "no packet for 15s" is not a liveness test. A session counts as stale after
    120s without any metadata packet, or once the extrapolated position runs more
    than 15s past the track duration. When stale but PulseAudio still shows a
    shairport sink-input, report "reconnecting"."""

    now = time.time()
    with _ap_lock:
        d = dict(_ap)
    if d.get("paused"):
        el = d.get("elapsed", 0.0)          # paused: the position is frozen, no extrapolation
    else:
        el = d.get("elapsed", 0.0) + max(0.0, now - d.get("elapsed_at", 0.0))
    dur = d.get("duration", 0.0)
    stale = ((not d.get("paused")
              and (now - d.get("updated_at", 0.0) > 120.0
                   or (dur > 0 and el > dur + 15.0)))
             or (d.get("paused") and not _ap_pa_playing()))
    if d.get("active") and not stale:
        if dur > 0:
            el = min(el, dur)
        d["elapsed"] = round(el, 1)
    elif _ap_pa_playing():
        # A shairport stream is still present in PulseAudio, so never clear the known
        # title or position: the staleness check would misfire two minutes into any
        # long track, because 3.3.8 only sends metadata on track changes.
        if d.get("active"):
            if dur > 0:
                d["elapsed"] = round(min(el, dur), 1)
            else:
                d["elapsed"] = round(min(el, 7200.0), 1)
        else:
            d = {"active": True, "title": "", "artist": "", "album": "",
                 "elapsed": 0.0, "duration": 0.0, "art_t": 0.0, "reconnecting": True}
    else:
        d["elapsed"] = 0.0
        d["active"] = False
    d.pop("updated_at", None)
    d.pop("elapsed_at", None)
    if d.get("active") and not d.get("art_t"):
        _, _, t = _ap_cache_art()        # no PICT on the pipe: use the cached cover mtime to drive reloads
        if t:
            d["art_t"] = t
    return d


def get_status():
    res = {"state": "stopped", "track": 0, "total_tracks": 1,
           "elapsed": 0.0, "duration": 0.0, "volume": 100, "song": None,
           "local": False, "repeat": False, "random": False,
           "single": False, "audio": None}
    rc, out = mpc("status")
    for line in out.splitlines():
        vm = re.search(r"volume:\s*(\d+)%", line)
        if vm:
            res["volume"] = int(vm.group(1))
        for key in ("repeat", "random", "single"):
            km = re.search(key + r":\s*(on|off)", line)
            if km:
                res[key] = km.group(1) == "on"
        tm = re.search(r"#(\d+)/(\d+)\s+(\d+):(\d+)/(\d+):(\d+)", line)
        if tm:
            res["track"] = int(tm.group(1))
            res["total_tracks"] = int(tm.group(2))
            res["elapsed"] = int(tm.group(3)) * 60 + int(tm.group(4))
            res["duration"] = int(tm.group(5)) * 60 + int(tm.group(6))
        sm = re.search(r"\[(playing|paused|stopped)\]", line)
        if sm:
            res["state"] = sm.group(1)
        cm = re.search(r"cdda://\S*?/(\d+)$", line)
        if cm:
            res["song"] = int(cm.group(1))
        am = re.search(r"audio:\s*(\d+):(\d+):(\d+)", line)
        if am:
            rate, depth, ch = (int(am.group(1)), int(am.group(2)),
                               int(am.group(3)))
            res["audio"] = {"rate": rate, "depth": depth, "ch": ch,
                            "kbps": round(rate * depth * ch / 1000)}
    link = probe_audio_link()
    res["link"] = link
    res["bt"] = bt_view()
    res["airplay"] = ap_view()
    # with the pulse output MPD omits the audio line; use the actual sink format
    if not res.get("audio") and link.get("fmt"):
        res["audio"] = link["fmt"]
    rc2, out2 = mpc("current", "-f", "%file%")
    f = (out2 or "").strip()
    if f and not f.startswith("cdda://"):
        res["local"] = True
        res["local_file"] = f
    return res


def _fmt_kbps(spec):
    """Parse a PulseAudio sample-format string such as 's16le 2ch 44100Hz'.

    Returns {rate, depth, ch, kbps}; s24-32le counts 24 significant bits. Returns
    None when the string cannot be parsed.
    """
    if not spec:
        return None
    m = re.search(r"(\d+)Hz", spec)
    rate = int(m.group(1)) if m else 0
    m = re.search(r"(\d+)ch", spec)
    ch = int(m.group(1)) if m else 0
    depth = None
    m = re.search(r"\bs(\d+)(?:-\d+)?(?:le|be)\b", spec)
    if m:
        depth = int(m.group(1))
    else:
        m = re.search(r"float(\d+)", spec)
        depth = int(m.group(1)) if m else None
    if rate and depth and ch:
        return {"rate": rate, "depth": depth, "ch": ch,
                "kbps": round(rate * depth * ch / 1000)}
    return None


def probe_audio_link(max_age=2.0):
    """Probe the current audio output path (PulseAudio).

    Returns:
      mode      "wired", "bt" or "none": which path the default sink uses
      sink      name of the current default sink
      desc      sink description (device name for Bluetooth, a fixed id for wired)
      codec     Bluetooth codec name such as aptX or SBC (Bluetooth only)
      kbps      typical codec link bitrate (Bluetooth only)
      fmt       running format of the default sink (only while audio flows)
      bt_sinks  connected Bluetooth sink names, used to decide if switching is possible
    Results are cached for 2s, since the UI polls status once a second.
    """
    now = time.time()
    with _link_lock:
        if _link_cache["v"] and now - _link_cache["t"] < max_age:
            return _link_cache["v"]
    res = {"mode": "none", "sink": None, "desc": None, "codec": None,
           "kbps": None, "fmt": None, "bt_sinks": []}
    try:
        rc, out = run(["pactl", "-f", "json", "info"], timeout=6)
        default = None
        if rc == 0:
            try:
                default = json.loads(out).get("default_sink_name")
            except Exception:
                pass
        rc2, out2 = run(["pactl", "-f", "json", "list", "sinks"], timeout=6)
        sinks = []
        if rc2 == 0:
            try:
                for s in json.loads(out2):
                    sinks.append({"name": s.get("name"),
                                  "desc": s.get("description"),
                                  "spec": s.get("sample_specification"),
                                  "state": s.get("state")})
            except Exception:
                pass
        bt = [s for s in sinks
              if (s["name"] or "").startswith(("bluez_sink.", "bluez_output"))]
        res["bt_sinks"] = [s["name"] for s in bt]
        active = None
        if default and default.startswith(("bluez_sink.", "bluez_output")) and bt:
            active = next((s for s in bt if s["name"] == default), bt[0])
            res.update(mode="bt", sink=active["name"], desc=active["desc"])
            prof = "a2dp-sink"
            bt_codec = None
            rc3, out3 = run(["pactl", "-f", "json", "list", "cards"], timeout=6)
            if rc3 == 0:
                try:
                    for c in json.loads(out3):
                        if (c.get("name") or "").startswith("bluez_card"):
                            props = c.get("properties") or {}
                            # PulseAudio exposes bluetooth.codec; PipeWire, which
                            # answers the same protocol, uses api.bluez5.codec
                            bt_codec = (props.get("bluetooth.codec")
                                        or props.get("api.bluez5.codec"))
                            prof = c.get("active_profile") or prof
                            break
                except Exception:
                    pass
            # the negotiated codec property (sbc / sbc_xq / aptx_hd / ldac) tracks
            # the codec more closely than active_profile does
            ck = ("a2dp-sink-" + bt_codec.replace("-", "_")) if bt_codec else None
            if ck and ck in BT_PROFILE_NAME:
                res["codec"] = BT_PROFILE_NAME[ck]
                res["kbps"] = BT_PROFILE_KBPS.get(ck)
            else:
                res["codec"] = BT_PROFILE_NAME.get(
                    prof, prof.replace("a2dp-sink-", "").upper() or "SBC")
                res["kbps"] = BT_PROFILE_KBPS.get(prof)
        elif default:
            active = next((s for s in sinks if s["name"] == default), None)
            res.update(mode="wired", sink=default,
                       desc=(active or {}).get("desc"))
        if active and active.get("state") == "RUNNING":
            res["fmt"] = _fmt_kbps(active.get("spec"))
    except Exception:
        pass
    with _link_lock:
        _link_cache.update(t=time.time(), v=res)
    return res


def wired_sink():
    """Name of the sink to switch back to when leaving Bluetooth.

    Returns the configured override when set, otherwise the first sink that is
    not a Bluetooth one -- on a stock install that is the USB or I2S DAC. None
    means no wired output is present, and callers must not switch to it.
    """
    if WIRED_SINK:
        return WIRED_SINK
    try:
        rc, out = run(["pactl", "-f", "json", "list", "sinks"], timeout=6)
        if rc == 0:
            for sink in json.loads(out):
                name = sink.get("name") or ""
                if not name.startswith(("bluez_sink.", "bluez_output")):
                    return name
    except Exception:
        logger.exception("Could not enumerate sinks")
    return None


def check_audio_server():
    """Report whether the audio server is reachable, and name the cause if not.

    A wrong XDG_RUNTIME_DIR, a unit running as an account that does not own the
    audio server, and a systemd sandbox that hides /run/user all end the same
    way: pactl cannot connect, so output switching answers "no wired output is
    available" and the real cause stays invisible. One probe at startup turns
    that into a line an operator can act on. Returns True when reachable.
    """
    rc, out = run(["pactl", "info"], timeout=6)
    if rc == 0:
        return True
    logger.error(
        "the audio server is unreachable: output switching and codec reporting "
        "will not work. XDG_RUNTIME_DIR is %r; it must be /run/user/<uid of the "
        "account this service runs as>. pactl said: %s",
        os.environ.get("XDG_RUNTIME_DIR"), (out or "").strip()[:400])
    return False


def play_track_cd(n):
    if type(n) is not int or not 1 <= n <= 99:
        return 1, "track numbers must be between 1 and 99"
    with _q_lock:
        return _mpd_run(["clear", "add " + mpd_quote("cdda://%s/%d" % (CD, n)), "play"])


def play_local(file_rel, album=False, dir_rel=None):
    try:
        if album:
            base = library_path(LIB, dir_rel)
            uris = [dir_rel + "/" + x.name for x in sorted(base.iterdir())
                    if x.suffix.lower() == ".flac" and x.is_file()]
            for uri in uris:
                library_path(LIB, uri)
        else:
            path = library_path(LIB, file_rel)
            if not path.is_file():
                return 1, "file not found"
            uris = [file_rel]
        if not uris:
            return 1, "the album has no playable tracks"
        cmds = ["clear"] + ["add " + mpd_quote(uri) for uri in uris] + ["play"]
    except (ValueError, OSError) as exc:
        return 1, str(exc)
    with _q_lock:
        return _mpd_run(cmds)


# ---------- Local music library ----------

_AEXT = {".flac": "FLAC", ".wav": "WAV", ".mp3": "MP3", ".m4a": "M4A",
         ".ogg": "OGG", ".opus": "OPUS", ".aac": "AAC"}


def _finfo(path):
    """Return (format, bytes) for a track: format from the extension, size from the file."""
    ext = os.path.splitext(path)[1].lower()
    fmt = _AEXT.get(ext, (ext.lstrip(".") or "?").upper())
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return fmt, size


def list_library():
    albums = []
    if not os.path.isdir(LIB):
        return albums
    for d in sorted(os.listdir(LIB)):
        full = os.path.join(LIB, d)
        if not os.path.isdir(full):
            continue
        files = sorted(x for x in os.listdir(full)
                       if x.lower().endswith((".flac", ".wav", ".mp3", ".m4a", ".ogg")))
        if not files:
            continue
        artist, album = None, None
        if " - " in d:
            artist, album = d.split(" - ", 1)
        tracks = []
        for i, f in enumerate(files):
            stem, _ = os.path.splitext(f)
            m = re.match(r"^(\d+)\s*-\s*(.+)$", stem)
            if m:
                n, title = int(m.group(1)), m.group(2).strip()
            else:
                n, title = i + 1, stem
            fmt, size = _finfo(os.path.join(full, f))
            tracks.append({"n": n, "file": d + "/" + f, "title": title,
                           "fmt": fmt, "size": size})
        albums.append({"dir": d, "artist": artist, "album": album,
                       "tracks": tracks})
    return albums


# ---------- Merged view of the disc in the drive and the local albums ----------

def _norm_key(s):
    """Normalisation key for album matching: lowercase, strip blanks, fold full-width characters.

    Metadata services and CD-Text return full-width punctuation for Japanese and
    Chinese releases while the local folder names use the ASCII forms, so both
    have to collapse to the same key for the shelf to match a ripped album.
    """
    s = (s or "").strip().lower()
    for a, b in (("\u3000", " "), ("\uff08", "("), ("\uff09", ")"), ("\uff1a", ":"),
                 ("\uff0c", ","), ("\uff03", "#"), ("\u2019", "'"),
                 ("\u201c", '"'), ("\u201d", '"')):
        s = s.replace(a, b)
    s = re.sub(r"\s+", "", s)
    return s


# Maps the romanised or traditional artist spelling returned by MusicBrainz and
# CD-Text onto the name used by the local collection. Add your own entries here:
# lowercased lookup key -> the name used by the local directory.
_ARTIST_ALIAS = {
    "example alias": "Example Artist",
}


def _norm_artist(s):
    s = (s or "").strip().lower()
    return _ARTIST_ALIAS.get(s, s)


def get_shelf():
    """Merge the shelf: the disc in the drive plus the local albums, without duplicates.

    Returns:
      cd     : None, or {artist, album, year, discid, local_dir, tracks:[...]},
                       tracks:[{n,title,dur,local_file}]}
              where local_dir is the album directory on disk and tracks[].local_file
              is the ripped file for that track, or None when only the CD has it.
      albums : the remaining local albums, excluding the one in the drive
      cd_err : error text for the drive or disc (a string when the tray is empty)
    """
    toc = get_toc_payload()
    albums = list_library()
    cd = None
    if not toc.get("err"):
        cd = {"artist": toc.get("artist"), "album": toc.get("album"),
              "year": toc.get("year"), "discid": toc.get("discid"),
              "local_dir": None, "tracks": []}
        cd_key = _norm_key("%s-%s" % (_norm_artist(cd["artist"]),
                                      cd["album"] or ""))
        matched = None
        if cd_key:
            for a in albums:
                ak = _norm_key("%s-%s" % (_norm_artist(a.get("artist")),
                                          a.get("album") or a.get("dir") or ""))
                if ak and ak == cd_key:
                    matched = a
                    break
            # the artist spelling may differ between the two sides; retry on album name alone
            if matched is None and cd.get("album"):
                cak = _norm_key(cd["album"])
                for a in albums:
                    ak = _norm_key(a.get("album") or a.get("dir") or "")
                    if ak and ak == cak:
                        matched = a
                        break
        local_by_n = {}
        if matched is not None:
            albums.remove(matched)
            cd["local_dir"] = matched["dir"]
            for t in matched["tracks"]:
                local_by_n[t["n"]] = t
        _off = int(toc.get("offset") or 0)
        for t in toc.get("tracks") or []:
            item = dict(t)
            lt = local_by_n.get(t["n"] + _off)
            if lt:
                item["local_file"] = lt["file"]
                item["fmt"] = lt.get("fmt", "FLAC")
                item["size"] = lt.get("size", 0)
            else:
                item["fmt"] = "CDDA"
                # CD-only track with no local file: estimate at 44.1k/16-bit stereo ~176400 B/s
                item["size"] = int((t.get("dur") or 0) * 176400)
            cd["tracks"].append(item)
    return {"cd": cd, "albums": albums, "cd_err": toc.get("err")}


# ---------- Whole-library play queue ----------

def shelf_entries():
    """Flat ordered view of the whole library: disc tracks in the drive first, using
    the local copy when one exists, then every track of every local album.

    Returns [{uri, title}] where uri can be handed straight to mpc add:
      cdda:///dev/sr0/N, or a path relative to the music library such as
      "Artist - Album/01 - Track Title.flac".
    The order matches the library view in the UI, with the disc pinned to the top.
    """
    s = get_shelf()
    out = []
    cd = s.get("cd")
    if cd:
        for t in cd.get("tracks") or []:
            nm = t.get("title") or "Track %d" % (t.get("n") or 0)
            if t.get("local_file"):
                out.append({"uri": t["local_file"], "title": nm})
            else:
                out.append({"uri": "cdda://%s/%d" % (CD, t.get("n") or 1),
                            "title": nm})
    for a in s.get("albums") or []:
        for t in a.get("tracks") or []:
            out.append({"uri": t["file"], "title": t.get("title") or ""})
    return out


def queue_and_play(uri):
    """Clear the queue, enqueue the whole library, then start playing at the given uri.

    Previous/next and the repeat and shuffle modes then operate across the whole
    library rather than within a single album or source.
    """
    with _q_lock:
        entries = shelf_entries()
        idx = None
        for i, e in enumerate(entries):
            if e["uri"] == uri:
                idx = i
                break
        if idx is None:
            return 1, "that track is not in the current library"
        try:
            cmds = ["clear"] + ["add " + mpd_quote(e["uri"]) for e in entries]
        except ValueError as exc:
            return 1, str(exc)
        cmds.append("play %d" % idx)
        rc, err = _mpd_run(cmds)
        return rc, err or "playing"


# ---------- Ripping ----------

def _sanitize(s):
    s = (s or "").strip()
    s = re.sub(r'[/\x00-\x1f\\]', "_", s)
    s = re.sub(r"\s+", " ", s).rstrip(" .")
    return s[:80] or "Unknown"


def _album_dirname(meta, discid):
    a = (meta or {}).get("artist")
    b = (meta or {}).get("album")
    if a and b:
        return "%s - %s" % (_sanitize(a), _sanitize(b))
    if b:
        return _sanitize(b)
    return "CD %s" % discid


def _rip_one_track(n, total, title, albumdir, meta, discid):
    """Rip one track to a tagged FLAC file. Returns (ok, err)."""
    artist = (meta or {}).get("artist") or "Unknown Artist"
    album = (meta or {}).get("album") or ("CD %s" % discid)
    year = (meta or {}).get("year") or ""
    final = str(library_path(LIB, albumdir + "/%02d - %s.flac" % (n, _sanitize(title))))
    if os.path.exists(final) and os.path.getsize(final) > 100000:
        return True, None
    directory = library_path(LIB, albumdir)
    directory.mkdir(parents=True, exist_ok=True)
    # Unique staging files never appear in the library; cleanup on every exit.
    with tempfile.TemporaryDirectory(prefix=".rip-", dir=directory) as staging:
        tmp_wav = os.path.join(staging, "track.wav")
        tmp_flac = os.path.join(staging, "track.flac")
        rc, out = run(["cdparanoia", "-d", CD, "-w", str(n), tmp_wav], timeout=600)
        if rc != 0:
            return False, "track %d read failed: %s" % (n, out.strip()[-200:])
        cmd = ["flac", "-f", "-o", tmp_flac,
               "-T", "TITLE=" + title, "-T", "ARTIST=" + artist,
               "-T", "ALBUM=" + album, "-T", "TRACKNUMBER=%02d" % n,
               "-T", "TRACKTOTAL=%02d" % total]
        if year:
            cmd += ["-T", "DATE=" + year]
        cmd.append(tmp_wav)
        rc, out = run(cmd, timeout=600)
        if rc != 0:
            return False, "track %d encode failed: %s" % (n, out.strip()[-200:])
        os.replace(tmp_flac, final)
    return True, None


def rip_worker(tracks, meta, discid, token=None):
    total = len(tracks)
    try:
        albumdir = _album_dirname(meta, discid)
        with _rip_lock:
            _rip.update(running=True, total=total, done=0, cur=None,
                        album=albumdir, err=None, finished_t=0.0)
        for i, (n, title) in enumerate(tracks):
            with _rip_lock:
                _rip["cur"] = "Track %d/%d: %s" % (i + 1, total, title)
            ok, err = _rip_one_track(n, total, title, albumdir, meta, discid)
            if not ok:
                with _rip_lock:
                    _rip["err"] = err
                break
            with _rip_lock:
                _rip["done"] = i + 1
        # tell MPD to rescan; always refresh, failed tracks simply leave no file
        rc, out = mpc("update", albumdir)
        if rc:
            with _rip_lock:
                _rip["err"] = _rip["err"] or ("library rescan failed: " + out[:200])
    except Exception as exc:
        logger.exception("Ripping failed")
        with _rip_lock:
            _rip["err"] = str(exc)[:300]
    finally:
        with _rip_lock:
            _rip["running"] = False
            _rip["cur"] = None
            if (not _rip.get("err") and total > 0
                    and _rip.get("done", 0) >= total):
                _rip["finished_t"] = time.time()   # the UI shows the "done" state from this
        if token is not None:
            _drive.release(token)


def start_rip(track_list):
    # Reserve before TOC I/O or thread creation, not inside the worker.
    with _rip_lock:
        if _rip["running"]:
            return False, "a rip is already running"
        _rip.update(running=True, total=0, done=0, cur=None,
                    album=None, err=None, finished_t=0.0)
    started = False
    token = None
    try:
        validate_action({"action": "rip", "tracks": track_list})
        payload = get_toc_payload()
        if payload["err"] or not payload["tracks"]:
            return False, payload["err"] or "no disc in the tray"
        discid = payload["discid"] or "unknown"
        meta = {"artist": payload["artist"], "album": payload["album"],
                "year": payload["year"]}
        if not payload["tracks"][0].get("title"):
            return False, "no track titles yet, so files cannot be named; check the network and retry"
        titles = {t["n"]: t.get("title") or ("Track %d" % t["n"])
                  for t in payload["tracks"]}
        selected = sorted(titles if track_list is None else track_list)
        if not selected or any(n not in titles for n in selected):
            return False, "that track is not on the current disc"
        jobs = [(n, titles[n]) for n in selected]
        token = _drive.claim("rip")
        if token is None:
            return False, "Optical drive busy, please wait"
        # Prevent MPD's queued CD tracks from starting while the ripper owns it.
        with _q_lock:
            rc, message = mpc("stop")
            if rc:
                return False, message or "playback could not be stopped, so the rip did not start"
            th = threading.Thread(target=rip_worker, args=(jobs, meta, discid, token), daemon=True)
            th.start()
        started = True
        return True, "ripping %d tracks" % len(jobs)
    except Exception as exc:
        logger.exception("Could not start ripping")
        with _rip_lock:
            _rip["err"] = str(exc)[:300]
        return False, str(exc)[:300]
    finally:
        if not started:
            if token is not None:
                _drive.release(token)
            with _rip_lock:
                _rip["running"] = False


def rip_status():
    with _rip_lock:
        return dict(_rip)


# ---------- Bluetooth management ----------

def btctl(args, timeout=15):
    """Run a single non-interactive bluetoothctl command. Returns (rc, output).

    Note that in non-interactive mode bluetoothctl keeps running until --timeout
    even after the command has finished, so the timeout is also a minimum runtime.
    That is fine for waiting commands such as pair, but use _btc_fast for commands
    that return immediately.
    """
    try:
        p = subprocess.run(
            ["bluetoothctl", "--timeout", str(timeout)] + list(args),
            capture_output=True, text=True, timeout=timeout + 10)
        text = p.stdout or ""
        if p.stderr:
            text += "\n" + p.stderr
        return p.returncode, text
    except subprocess.TimeoutExpired:
        return -1, "bluetoothctl timed out"
    except Exception as e:
        return -2, str(e)


def _btc_fast(args, stall=0.6, hard=6):
    """Bounded byte reads preserve the quiet-output completion heuristic."""
    try:
        rc, output = run_until_quiet(["bluetoothctl"] + list(args), quiet=stall, timeout=hard)
    except Exception as exc:
        return -2, str(exc)
    if rc == 0 and re.search(r"(?:Failed|not available|No default controller)", output, re.I):
        rc = 1
    return rc, output


def _bt_light():
    """Light refresh, throttled to 4s in the background: adapter power and known names.

    Per-device info calls are skipped because they cost a dozen extra bluetoothctl
    processes per refresh; paired state comes from the last snapshot, while connected
    state is inferred from PulseAudio sinks in bt_view.
    """
    try:
        with _bt_lock:
            old = {d["mac"]: d for d in _bt_state.get("devices", [])}
        rc, out = _btc_fast(["show"])
        if rc:
            raise RuntimeError(out.strip()[:200] or "querying the Bluetooth adapter failed")
        m = re.search(r"Powered:\s*(yes|no)", out)
        if not m:
            raise RuntimeError("the Bluetooth adapter reported no power state")
        powered = m.group(1) == "yes"
        devs = []
        if powered:
            rc2, out2 = _btc_fast(["devices"])
            if rc2:
                raise RuntimeError(out2.strip()[:200] or "listing Bluetooth devices failed")
            if rc2 == 0:
                for line in out2.splitlines():
                    m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)",
                                 line.strip())
                    if m:
                        mac = m.group(1)
                        name = m.group(2).strip() or mac
                        prev = old.get(mac, {})
                        devs.append({"mac": mac, "name": name,
                                     "paired": bool(prev.get("paired")),
                                     "connected": bool(prev.get("connected"))})
        with _bt_lock:
            _bt_state.update(powered=powered, devices=devs, err=None,
                             t=time.monotonic())
    except Exception as e:
        with _bt_lock:
            _bt_state["err"] = str(e)
            _bt_state["t"] = time.monotonic()


def _bt_info_one(mac, name):
    """Turn one device info block into a dict. Separate so the pool can parallelise."""
    d = {"mac": mac, "name": name or mac, "paired": False,
         "connected": False}
    rc, out = _btc_fast(["info", mac])
    if rc == 0:
        for il in out.splitlines():
            pm = re.search(r"Paired:\s*(yes|no)", il)
            if pm:
                d["paired"] = pm.group(1) == "yes"
            cm = re.search(r"Connected:\s*(yes|no)", il)
            if cm:
                d["connected"] = cm.group(1) == "yes"
            nm = re.search(r"Alias:\s*(.+)", il)
            if nm and nm.group(1).strip():
                d["name"] = nm.group(1).strip()
    return d


def _bt_flag(mac, pattern):
    """Read one Paired:/Connected: flag, used to verify the outcome of an action.

    bluetoothctl does not report success consistently, so checking the resulting
    state is more reliable than parsing its output.
    """
    rc, out = _btc_fast(["info", mac])
    return rc == 0 and bool(re.search(pattern, out))


def _bt_wait_seen(mac, timeout=15):
    """Make sure the device sits in the BlueZ cache. If it does not, run a short scan

    so that it starts advertising again: BlueZ evicts unpaired devices within
    seconds of discovery ending, and some speakers pause and resume advertising.
    A device has to be fresh before pairing, otherwise pair answers
    "Device not available" or ConnectionAttemptFailed.
    """
    if _btc_fast(["info", mac])[0] == 0:
        return True
    p = subprocess.Popen(["bluetoothctl", "scan", "on"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if _btc_fast(["devices", mac])[1].find(mac) >= 0:
                return True
            time.sleep(1.5)
        return False
    finally:
        terminate_and_wait(p, grace=3)


def _bt_pa_connected(mac):
    """Whether this MAC already has a PulseAudio Bluetooth sink, meaning the audio
    path is ready. pactl does not clash with bluetoothctl, so it works as a reliable
    completion signal for waiting commands."""
    return mac in _sink_macs()


def _bt_wait_done(mac, want_conn, cmd_args, timeout=20, interval=1.0):
    """Run a waiting bluetoothctl command while polling for the PulseAudio sink.

    connect and disconnect keep hanging until --timeout after they have finished, and
    calling bluetoothctl info while they run is unreliable, so the appearance or
    disappearance of the bluez sink is used as the completion signal instead: the
    child is terminated as soon as it appears, instead of stretching out the action.
    """
    p = subprocess.Popen(
        ["bluetoothctl", "--timeout", str(timeout)] + list(cmd_args),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if _bt_pa_connected(mac) == want_conn:
                break
            time.sleep(interval)
    finally:
        terminate_and_wait(p, grace=3)
    return _bt_pa_connected(mac) == want_conn


def _bt_full():
    """Full snapshot, taken on first use and after pairing or power changes."""
    try:
        rc, out = _btc_fast(["show"])
        if rc:
            raise RuntimeError(out.strip()[:200] or "querying the Bluetooth adapter failed")
        m = re.search(r"Powered:\s*(yes|no)", out)
        if not m:
            raise RuntimeError("the Bluetooth adapter reported no power state")
        powered = m.group(1) == "yes"
        devs = []
        if powered:
            rc2, out2 = _btc_fast(["devices"])
            if rc2:
                raise RuntimeError(out2.strip()[:200] or "listing Bluetooth devices failed")
            if rc2 == 0:
                rows = []
                for line in out2.splitlines():
                    m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)",
                                 line.strip())
                    if m:
                        rows.append((m.group(1), m.group(2).strip()))
                with ThreadPoolExecutor(max_workers=4) as ex:
                    devs = list(ex.map(
                        lambda r: _bt_info_one(*r), rows[:15]))
        with _bt_lock:
            _bt_state.update(powered=powered, devices=devs, err=None,
                             t=time.monotonic())
    except Exception as e:
        with _bt_lock:
            _bt_state["err"] = str(e)
            _bt_state["t"] = time.monotonic()


_bt_refresh = RefreshWorker(
    lambda: _bt_light() if _bt_state.get("t") else _bt_full(), lambda: _bt_full())


def _ensure_bt():
    """A cold status request returns immediately while one full probe runs."""
    with _bt_lock:
        if _bt_state.get("discovering"):
            return
        if time.monotonic() - _bt_state.get("t", 0) <= BT_SHOW_S:
            return
    _bt_refresh.request()


def _sink_macs():
    """MAC addresses of the devices that currently have a PulseAudio Bluetooth sink."""
    out = set()
    link = probe_audio_link(max_age=1.5)
    for s in (link or {}).get("bt_sinks") or []:
        part = s.split(".")[1] if s.count(".") >= 2 else ""
        if "_" in part:
            out.add(part.replace("_", ":"))
    return out


def bt_view():
    """Return a copy of the Bluetooth state cache. Never blocks."""
    _ensure_bt()
    with _bt_lock:
        powered = _bt_state.get("powered")
        discovering = bool(_bt_state.get("discovering"))
        err = _bt_state.get("err")
        devices = [dict(d) for d in _bt_state.get("devices", [])]
        busy = dict(_bt_busy) if _bt_busy.get("mac") else None
        last = dict(_bt_last) if time.time() - _bt_last.get("t", 0) < 6 \
            else None
    if powered:
        active = _sink_macs()
        for d in devices:
            d["connected"] = d.get("mac") in active
    return {"powered": powered, "discovering": discovering,
            "err": err, "busy": busy, "last": last, "devices": devices}


def _bt_full_async():
    """Refresh fully after an action without blocking the response; the UI polls anyway."""
    _bt_refresh.request(full=True)


def bt_set_power(on):
    """Power the Bluetooth adapter on or off, switching to wired output first."""
    if not on:
        link = probe_audio_link(max_age=0)
        if link.get("mode") == "bt":
            target = wired_sink()
            if target:
                run(["pactl", "set-default-sink", target], timeout=6)
    rc, out = _btc_fast(["power", "on" if on else "off"])
    if rc != 0 or "succeeded" not in (out or ""):
        return 1, (out or "").strip()[:100] or "the Bluetooth power command failed"
    _bt_full_async()
    return 0, "Bluetooth " + ("on" if on else "off")


def _bt_ingest_scan_line(line):
    """Merge NEW/DEL device events from bluetoothctl scan output into the list live.

    The UI polls once a second, so devices appear as they are discovered instead of
    all at once when the scan ends. CHG events such as RSSI changes are ignored so
    that the list does not flicker.
    """
    ln = _BT_CTRL_RE.sub("", line or "").strip()
    m = re.search(r"(NEW|DEL)\s+Device\s+([0-9A-F:]{17})(?:\s+(.*))?$", ln)
    if not m:
        return
    kind, mac, name = m.group(1), m.group(2), (m.group(3) or "").strip()
    with _bt_lock:
        devs = _bt_state["devices"]
        idx = next((i for i, d in enumerate(devs) if d["mac"] == mac), None)
        if kind == "DEL":
            if idx is not None:
                devs.pop(idx)
            return
        if idx is None:
            devs.append({"mac": mac, "name": name or mac,
                         "paired": False, "connected": False})
        elif name and name != mac:
            devs[idx]["name"] = name


def _bt_scan_worker():
    """Background scan: a fixed window, streaming new devices in as they appear.

    bluetoothctl scan on is a waiting command and can hang for 25-35s, with the
    device list only refreshed once it exits. Instead this starts scan on with Popen,
    merges NEW/DEL events from stdout as they arrive, terminates the child after
    BT_SCAN_S, then runs scan off and recalibrates the list from the lighter probe.
    """
    with _bt_lock:
        _bt_state["discovering"] = True
        _bt_state["err"] = None
    try:
        _btc_fast(["scan", "off"])   # reset any discovery left over from a previous run
    except Exception:
        pass
    proc = None
    reader_thread = None
    stop = {"go": True}
    try:
        proc = subprocess.Popen(["bluetoothctl", "scan", "on"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True, bufsize=1)

        def reader():
            try:
                for line in proc.stdout:
                    if not stop["go"]:
                        break
                    _bt_ingest_scan_line(line)
            except Exception:
                pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        time.sleep(BT_SCAN_S)               # # discovery window
    except Exception:
        pass
    finally:
        stop["go"] = False
        if proc is not None:
            terminate_and_wait(proc, grace=3)
            if reader_thread is not None and reader_thread.ident is not None:
                reader_thread.join(timeout=2)
            proc.stdout.close()
    try:
        _btc_fast(["scan", "off"])   # make sure BlueZ discovery has stopped
    except Exception:
        pass
    # Recalibrate from the controller cache, keeping the remembered paired state;
    # newly discovered devices are usually unpaired, and _bt_full refines their state
    # once the user presses a button.
    _bt_light()
    with _bt_lock:
        _bt_state["discovering"] = False


def bt_scan():
    """Scan for BT_SCAN_S seconds in the background, listing devices as they appear."""
    with _bt_lock:
        if _bt_state.get("discovering"):
            return 1, "a scan is already running"
        _bt_state["discovering"] = True
    try:
        threading.Thread(target=_bt_scan_worker, daemon=True).start()
    except Exception as exc:
        with _bt_lock:
            _bt_state["discovering"] = False
            _bt_state["err"] = str(exc)
        return 1, str(exc)
    return 0, "scanning (about 6 seconds; devices appear as they are found)"


def _bt_connect_worker(mac, want_pair):
    """Run pairing and connecting in the background, then set the result banner.

    The pair stage relies on the bluetoothctl timeout because no sink exists yet to
    signal completion, and info clashes while it runs; connect polls the sink instead.
    The device is refreshed into the cache before pairing, and some speakers reject
    the first attempt (ConnectionAttemptFailed), so a couple of retries are normal.
    """
    ok, msg = False, ""
    try:
        already = _bt_flag(mac, r"Paired:\s*yes")
        if want_pair and not already:
            _bt_wait_seen(mac)
            paired = False
            for attempt in (1, 2, 3):
                btctl(["pair", mac], timeout=25)
                if _bt_flag(mac, r"Paired:\s*yes"):
                    paired = True
                    break
                time.sleep(2)          # let the device reset, then retry
                _bt_wait_seen(mac)
            if not paired:
                msg = "pairing failed after 3 attempts; make sure the speaker is in pairing mode"
                return
            _btc_fast(["trust", mac])
        else:
            _btc_fast(["trust", mac])
        connected = False
        for attempt in (1, 2, 3):
            if _bt_wait_done(mac, True, ["connect", mac], timeout=25):
                connected = True
                break
            time.sleep(2)              # the first connect is often refused; retry
        if connected:
            ok, msg = True, "connected " + mac
        else:
            msg = "connecting failed after 3 attempts; make sure the speaker is powered on"
    except Exception as e:
        msg = str(e)[:100]
    finally:
        with _bt_lock:
            _bt_busy.update(mac=None, act=None, msg="", t=time.time())
            _bt_last.update(ok=ok, msg=msg, t=time.time())
        _bt_full_async()


def _bt_disconnect_worker(mac):
    """Disconnect in the background, switching to wired output first when needed."""
    ok, msg = False, ""
    try:
        link = probe_audio_link(max_age=0)
        if link.get("mode") == "bt":
            target = wired_sink()
            if target:
                run(["pactl", "set-default-sink", target], timeout=6)
        if _bt_wait_done(mac, False, ["disconnect", mac], timeout=15):
            ok, msg = True, "disconnected " + mac
        else:
            msg = "disconnecting failed; the device is still connected"
    except Exception as e:
        msg = str(e)[:100]
    finally:
        with _bt_lock:
            _bt_busy.update(mac=None, act=None, msg="", t=time.time())
            _bt_last.update(ok=ok, msg=msg, t=time.time())
        _bt_full_async()


def _bt_run_busy(mac, act, worker_fn, ok_msg):
    """Common entry point for async actions: claim busy, start a worker, return at once."""
    with _bt_lock:
        if _bt_busy.get("mac"):
            return 1, "another device action is already running"
        _bt_busy.update(mac=mac, act=act, msg="", t=time.time())
        _bt_last.update(ok=False, msg="", t=0.0)   # clear the previous banner
        _bt_state["err"] = None
    try:
        threading.Thread(target=worker_fn, daemon=True).start()
    except Exception as exc:
        with _bt_lock:
            _bt_busy.update(mac=None, act=None, msg="", t=time.time())
            _bt_last.update(ok=False, msg=str(exc), t=time.time())
        return 1, str(exc)
    return 0, ok_msg


def bt_connect_one(mac, want_pair=True):
    """Pair and connect one device (uppercase MAC), returning immediately.

    want_pair=True with an unpaired device runs pair, trust and connect; an already
    paired device is simply reconnected. While this runs the button for that device
    shows "Connecting..." and is disabled; the result lands in the bt.last banner.
    """
    act = "pair" if want_pair else "connect"
    return _bt_run_busy(mac, act,
                        lambda: _bt_connect_worker(mac, want_pair),
                        "%s in the background; the view refreshes when it finishes (about 10-25s)" % (
                            "Pairing and connecting" if want_pair else "Connecting"))


def bt_disconnect_one(mac):
    """Disconnect a device but keep it paired, returning immediately.

    disconnect is a waiting command and info clashes while it runs, so the removed
    sink is used as the completion signal in _bt_disconnect_worker.
    """
    return _bt_run_busy(mac, "disconnect",
                        lambda: _bt_disconnect_worker(mac),
                        "Disconnecting in the background; the view refreshes when it finishes")


# ---------- Action dispatch ----------

def do_action(data):
    try:
        validate_action(data)
    except RequestError as exc:
        return 1, str(exc)
    act = data["action"]
    transport = {"play", "pause", "toggle", "stop", "prev", "next", "seek", "mode",
                 "play_track", "play_at", "play_local", "play_album", "play_cd_disc", "rip", "reidentify"}
    if act in transport:
        with _q_lock:
            if act in {"play", "toggle", "prev", "next", "seek", "play_track", "play_at", "play_cd_disc"}:
                token = _drive.claim("playback")
                if token is None:
                    return 1, "the optical drive is busy; wait for the current operation"
                try:
                    return _do_action(data)
                finally:
                    _drive.release(token)
            return _do_action(data)
    return _do_action(data)


def _do_action(data):
    try:
        validate_action(data)
    except RequestError as exc:
        return 1, str(exc)
    act = data.get("action")
    if act == "play_track":
        return play_track_cd(int(data.get("track", 1)))
    if act == "play_at":
        # whole-library playback: uri is cdda:///dev/sr0/N or a library-relative path
        uri = data.get("uri")
        if not uri:
            return 1, "uri is required"
        return queue_and_play(uri)
    if act == "prev":
        return mpc("prev")
    if act == "next":
        return mpc("next")
    if act == "play_local":
        f = data.get("file")
        if not f:
            return 1, "file is required"
        return play_local(f)
    if act == "play_album":
        d = data.get("dir")
        if not d:
            return 1, "dir is required"
        return play_local(None, album=True, dir_rel=d)
    if act == "rip":
        tl = data.get("tracks")
        ok, msg = start_rip(tl)
        return (0, msg) if ok else (1, msg)
    if act == "play":
        return mpc("play")
    if act == "pause":
        return mpc("pause")
    if act == "toggle":
        st = get_status()["state"]
        return mpc("pause") if st == "playing" else mpc("play")
    if act == "stop":
        return mpc("stop")
    if act == "volume":
        v = max(0, min(100, int(data.get("volume", 50))))
        return mpc("volume", str(v))
    if act == "seek":
        # seeking from the progress bar: the native seekcur command (integer seconds) is
        # preferred, with mpc seek as a fallback. Direct CD tracks are seekable too.
        try:
            pos = max(0, int(data.get("pos")))
        except (TypeError, ValueError):
            return 1, "invalid seek argument; integer seconds are required"
        rc, err = _mpd_run(["seekcur %d" % pos])
        if rc == 0:
            return 0, "seeked"
        rc2, out2 = mpc("seek", str(pos))
        return rc2, ("seeked" if rc2 == 0
                     else (out2 or err or "seek failed").strip()[:100])
    if act == "mode":
        key = data.get("key")
        if key not in ("repeat", "random", "single"):
            return 1, "invalid mode argument"
        on = bool(data.get("on"))
        if key == "single" and on:
            # MPD semantics: single only repeats one track when repeat is also on
            # (single on with repeat off just stops at the end of the track)
            rc, msg = mpc("single", "on")
            if rc:
                return rc, msg
            rc, msg = mpc("repeat", "on")
        else:
            rc, msg = mpc(key, "on" if on else "off")
        if rc:
            return rc, msg
        return 0, "mode %s=%s" % (key, "on" if on else "off")
    if act == "play_cd_disc":
        # play the whole CD directly, without the local library, in disc order
        toc = get_toc_payload()
        if toc.get("err") or not toc.get("tracks"):
            return 1, toc.get("err") or "no disc in the tray"
        with _q_lock:
            cmds = ["clear"]
            cmds += ['add "cdda://%s/%d"' % (CD, t["n"])
                     for t in toc["tracks"]]
            cmds.append("play")
            rc, err = _mpd_run(cmds)
            return rc, err or "playing"
    if act == "switch_output":
        # switch between wired and Bluetooth by changing the default sink and moving
        to = data.get("to")
        link = probe_audio_link(max_age=0)
        if to == "bt":
            if not link.get("bt_sinks"):
                return 1, "no Bluetooth speaker is connected; pair and connect one first"
            target = link["bt_sinks"][0]
            name = "Bluetooth"
        elif to == "wired":
            target = wired_sink()
            if not target:
                return 1, "no wired output is available"
            name = "wired"
        else:
            return 1, "invalid switch_output argument; use to=wired or to=bt"
        rc, err = run(["pactl", "set-default-sink", target], timeout=6)
        if rc != 0:
            return 1, err or "changing the default output failed"
        # any stream that is already playing has to be moved explicitly
        rc2, out2 = run(["pactl", "-f", "json", "list", "sink-inputs"],
                        timeout=6)
        if rc2 == 0:
            try:
                for si in json.loads(out2):
                    sid = si.get("index")
                    if isinstance(sid, int):
                        run(["pactl", "move-sink-input", str(sid), target],
                            timeout=6)
            except Exception:
                pass
        return 0, "switched to %s output" % name
    if act == "bt_power":
        return bt_set_power(bool(data.get("on")))
    if act == "bt_scan":
        return bt_scan()
    if act == "bt_pair":
        mac = (data.get("mac") or "").strip().upper()
        if not mac or not _BT_ADDR_RE.fullmatch(mac):
            return 1, "a valid device MAC address is required"
        return bt_connect_one(mac, want_pair=True)
    if act == "bt_connect":
        mac = (data.get("mac") or "").strip().upper()
        if not mac or not _BT_ADDR_RE.fullmatch(mac):
            return 1, "a valid device MAC address is required"
        return bt_connect_one(mac, want_pair=False)
    if act == "bt_disconnect":
        mac = (data.get("mac") or "").strip().upper()
        if not mac or not _BT_ADDR_RE.fullmatch(mac):
            return 1, "a valid device MAC address is required"
        return bt_disconnect_one(mac)
    if act == "reidentify":
        token = _drive.claim("identify")
        if token is None:
            return 1, "Optical drive busy, please wait"
        try:
            return _reidentify()
        finally:
            _drive.release(token)
    return 1, "unknown action"


def _reidentify():
    # manual re-identify: stop playback to release the drive, force a re-query, fill
    rc, message = mpc("stop")
    if rc:
        return rc, message
    discid, fields, iderr = cdmeta.disc_id()
    if iderr:
        return 1, iderr
    meta, metaerr = cdmeta.resolve(discid, fields, force=True)
    if metaerr:
        _meta_mem.update(discid=None, meta=None, t=0.0)
        with _toc_lock:
            _toc.update(t=0.0, payload=None)
        return 1, "identification failed: %s" % metaerr
    if not meta or not meta.get("tracks"):
        _meta_mem.update(discid=None, meta=None, t=0.0)
        with _toc_lock:
            _toc.update(t=0.0, payload=None)
        return 1, "no track information found; the disc has no CD-Text and is not in any database"
    _meta_mem.update(discid=discid, meta=meta, t=time.monotonic())
    # Invalidate the old TOC before rebuilding with the new metadata.
    with _toc_lock:
        _toc.update(t=0.0, payload=None)
    _read_toc_payload()
    src = meta.get("src", "?")
    title = ((meta.get("artist") or "?") + " — " if meta.get("artist")
             else "") + (meta.get("album") or "?")
    return 0, "identified %d tracks: %s (source: %s)" % (len(meta["tracks"]), title, src)


def fmt(sec):
    sec = int(sec or 0)
    return "%d:%02d" % (sec // 60, sec % 60)


# ---------- Frontend page ----------

PAGE = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/toc":
            return self._send(200, json.dumps(get_toc_payload(), ensure_ascii=False))
        if self.path == "/api/status":
            return self._send(200, json.dumps(get_status()))
        if self.path == "/api/library":
            return self._send(200, json.dumps(
                {"albums": list_library()}, ensure_ascii=False))
        if self.path == "/api/shelf":
            return self._send(200, json.dumps(get_shelf(), ensure_ascii=False))
        if self.path == "/api/ripstatus":
            return self._send(200, json.dumps(rip_status(), ensure_ascii=False))
        if self.path.startswith("/api/airplay_art"):
            with _ap_lock:
                data, mime = _ap_art["data"], _ap_art["mime"]
            if not data:                     # no PICT on the pipe: fall back to the disk cache
                data, mime, _ = _ap_cache_art()
            if data:
                return self._send(200, data, mime)
            return self._send(404, json.dumps({"err": "no art"}))
        if self.path == "/favicon.ico":
            return self._send(204, "")
        return self._send(404, json.dumps({"err": "not found"}))

    def do_POST(self):
        if self.path == "/api/action":
            try:
                data = read_action(self.headers, self.rfile)
                rc, msg = do_action(data)
                status = 200
            except RequestError as exc:
                status, rc, msg = exc.status, 1, str(exc)
            except TimeoutError:
                status, rc, msg = 408, 1, "timed out while reading the request"
            except Exception:
                logger.exception("Action request failed")
                status, rc, msg = 500, 1, "the action failed; see the service log"
            return self._send(status, json.dumps({"rc": rc, "msg": str(msg)[:300]},
                                                ensure_ascii=False))
        return self._send(404, json.dumps({"err": "not found"}))


def main():
    """Serve the control UI and start the AirPlay metadata worker."""
    print("airplay-hub listening on http://%s:%d  (LIB=%s)" % (HOST, PORT, LIB), flush=True)
    check_audio_server()
    threading.Thread(target=_ap_meta_worker, daemon=True).start()
    with PlayerHTTPServer((HOST, PORT), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

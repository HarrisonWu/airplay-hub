"""Disc identification and metadata lookup.

A disc is resolved in three tiers, stopping at the first that yields a track
list: CD-Text read from the disc, then MusicBrainz, then freedb/CDDB. CD-Text
wins because it travels with the physical disc; MusicBrainz is preferred over
CDDB because it returns structured JSON rather than a flat handshake.

  - CD-Text: ``cdrdao read-toc`` through :func:`cdtext_lookup`.
  - MusicBrainz: ``/ws/2/discid/<mbid>`` with ``recordings+artist-credits``.
    The API requires a descriptive User-Agent and rate-limits requests.
  - freedb/CDDB: the gnudb HTTP gateway. The gnudb TCP service on port 8880 is
    retired, and the gateway handshake only answers clients it recognises, so
    the greeting impersonates a known client.

Results are cached per disc ID as JSON under ``$AIRPLAY_HUB_DATA_DIR/meta`` with
the winning tier recorded in ``src``, so the UI stays responsive and a manual
"re-identify" is the only thing that re-queries upstream.
"""
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .storage import atomic_json

logger = logging.getLogger(__name__)

DEVICE = "/dev/sr0"
GATEWAY = "http://gnudb.gnudb.org/~cddb/cddb.cgi"
HELLO = "abcde@example.com orangepi abcde 2.8.0"   # the app name must be a known client
CACHE_DIR = os.path.join(os.environ.get("AIRPLAY_HUB_DATA_DIR", "/opt/cdplayer"), "meta")
CACHE_TTL = 7 * 86400
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# MusicBrainz; the User-Agent has to carry a contact address
MB_API = "https://musicbrainz.org/ws/2"
MB_UA = "airplay-hub/1.0 ( http://orangepi.local:8080 )"
MB_INC = "recordings+artist-credits+release-groups+discids"


def disc_id():
    """Return (freedb_id, fields, err) where fields = [ntracks, off1..offN, total]."""
    try:
        p = subprocess.run(["cd-discid", DEVICE], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return None, None, "cd-discid failed: %s" % e
    if p.returncode != 0:
        return None, None, "cannot read the disc (empty tray?)"
    parts = p.stdout.split()
    if len(parts) < 3:
        return None, None, "cd-discid returned unexpected output"
    return parts[0], parts[1:], None


def _gate(cmd, timeout=15):
    """Call the HTTP CDDB gateway and return the decoded text."""
    params = urllib.parse.urlencode({
        "cmd": cmd,
        "hello": HELLO,
        "proto": "6",
    })
    req = urllib.request.Request(GATEWAY + "?" + params)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = _bounded_read(r)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _split_lines(text):
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s and s != ".":
            out.append(s)
    return out


def _bounded_read(response):
    value = response.read(MAX_RESPONSE_BYTES + 1)
    if len(value) > MAX_RESPONSE_BYTES:
        raise ValueError("metadata response too large")
    return value


def freedb_lookup(discid, fields):
    """CDDB query + read. Returns a meta dict, None when not found, or {"err": ...}."""
    ntracks = int(fields[0])
    offsets = " ".join(fields[1:-1])
    total = fields[-1]
    try:
        resp = _gate("cddb query %s %s %s %s"
                     % (discid, ntracks, offsets, total))
    except Exception as e:
        return {"err": "query failed: %s" % e}

    lines = _split_lines(resp)
    if not lines:
        return {"err": "the gateway did not respond"}
    code = lines[0]
    if code.startswith(("200 ", "210 ", "211 ")):
        # 200 puts the match on the status line; 210/211 use following lines.
        candidates = [code[4:]] if code.startswith("200 ") else lines[1:]
        cat = None
        for ln in candidates:
            t = ln.split()
            if (len(t) >= 2 and re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", t[0])
                    and re.fullmatch(r"[a-fA-F0-9]{8}", t[1])):
                cat, dbid = t[0], t[1]
                break
        if not cat:
            return None
    else:
        return None

    try:
        resp2 = _gate("cddb read %s %s" % (cat, dbid))
    except Exception as e:
        return {"err": "reading the record failed: %s" % e}
    lines2 = _split_lines(resp2)
    if not lines2 or not lines2[0].startswith("2"):
        return None
    meta = _parse_xmcd("\n".join(lines2))
    if not meta["tracks"]:
        return None
    meta["discid"] = discid
    meta["cat"] = cat
    return meta


def _parse_xmcd(payload):
    meta = {"album": None, "artist": None, "year": None, "tracks": []}
    titles = {}
    for line in payload.splitlines():
        line = line.strip()
        if line.startswith("DTITLE="):
            raw = line[7:].strip()
            if " / " in raw:
                artist, album = raw.split(" / ", 1)
            else:
                artist, album = None, raw
            meta["artist"] = (artist or "").strip() or None
            meta["album"] = (album or "").strip() or None
        elif line.startswith("DYEAR="):
            meta["year"] = line[6:].strip() or None
        elif line.startswith("TTITLE"):
            m = _track(line)
            if m:
                titles[m[0]] = m[1].strip()
    n = len(titles)
    for i in range(n):
        meta["tracks"].append(titles.get(i, "Track %d" % (i + 1)))
    return meta


def _track(line):
    try:
        head, title = line.split("=", 1)
        return int(head[len("TTITLE"):]), title
    except Exception:
        return None


def _cache_path(discid):
    if not isinstance(discid, str) or not re.fullmatch(r"[a-fA-F0-9]{8}", discid):
        raise ValueError("invalid DiscID")
    return os.path.join(CACHE_DIR, discid + ".json")


# ---------- CD-Text (stored on the disc; the best offline source) ----------

def _cdtext_parse(toc_text):
    """Parse cdrdao read-toc output into a meta dict, or None.

    CD-Text is written onto the disc at pressing time and is unaffected by
    database errors.
    """
    m = re.search(r'^\s*TITLE\s+"([^"]*)"', toc_text, re.M)
    album = m.group(1).strip() if m else None
    m = re.search(r'^\s*PERFORMER\s+"([^"]*)"', toc_text, re.M)
    artist = m.group(1).strip() if m else None
    if not album and not artist:
        return None          # no CD-Text (most unofficial pressings omit it)
    tracks = []
    # Per-track CD_TEXT blocks: look for TITLE after "// Track N"
    for sec in re.split(r"^//\s*Track\s+\d+", toc_text, flags=re.M)[1:]:
        tm = re.search(r'CD_TEXT\s*\{(.*?)\n\s*\}', sec, re.S)
        if tm:
            tm2 = re.search(r'TITLE\s+"([^"]*)"', tm.group(1))
            if tm2:
                tracks.append(tm2.group(1).strip())
    if not tracks:
        # A few discs list every title in the first block; fall back to the
        # album TITLE rule for each track's CD_TEXT
        for sec in re.split(r"^//\s*Track\s+\d+", toc_text, flags=re.M)[1:]:
            tm = re.search(r'TITLE\s+"([^"]*)"', sec)
            if tm:
                tracks.append(tm.group(1).strip())
    if not tracks:
        return None
    meta = {"album": album, "artist": artist, "year": None,
            "tracks": tracks, "src": "cdtext"}
    return meta


def cdtext_lookup(timeout=30):
    """Read CD-Text from the current disc with cdrdao.

    Returns (meta_or_None, err_or_None). The service user belongs to the cdrom
    and audio groups, so it can issue SCSI reads against /dev/sr0 directly.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="airplay-hub-toc-") as directory:
            tmp = os.path.join(directory, "disc.toc")
            p = subprocess.run(
                ["cdrdao", "read-toc", "--fast-toc", "--device", DEVICE, tmp],
                capture_output=True, text=True, timeout=timeout)
            if p.returncode != 0 or not os.path.exists(tmp):
                return None, None
            with open(tmp, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            return _cdtext_parse(text), None
    except subprocess.TimeoutExpired:
        return None, None
    except Exception:
        return None, None


# ---------- MusicBrainz ----------
def mb_discid_from_device():
    """Read a MusicBrainz-format DiscID with libdiscid; returns (mbid_or_None, err)."""
    try:
        import libdiscid
    except Exception:
        return None, "libdiscid is unavailable"
    try:
        d = libdiscid.read(DEVICE)
        return d.id, None
    except Exception as e:
        return None, "libdiscid read failed: %s" % e


def _mb_get(url, timeout=25):
    req = urllib.request.Request(
        url, headers={"User-Agent": MB_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(_bounded_read(r).decode("utf-8", "replace"))
    if not isinstance(result, dict):
        raise ValueError("the MusicBrainz response must be a JSON object")
    return result


def _artist_string(ac):
    """Flatten the artist-credit list into a 'Lead (feat. X)' string."""
    if not ac:
        return None
    out = ""
    for part in ac:
        if isinstance(part, dict):
            out += part.get("name", "")
            out += part.get("joinphrase", "")
        else:
            out += str(part)
    return out.strip() or None


def _mb_medium(release, discid, ntracks):
    """Select one physical disc, never concatenate a multi-disc release.

    Disc IDs outrank count-only matches. If IDs are absent, accept only one
    plausible medium in that release; ambiguous results use the CDDB fallback.
    """
    media = [m for m in release.get("media", []) if isinstance(m, dict)]
    exact = [m for m in media if any(d.get("id") == discid
             for d in (m.get("discs") or []) if isinstance(d, dict))]
    candidates = exact or [m for m in media if not m.get("discs")
                           and (not ntracks or len(m.get("tracks") or []) == ntracks)]
    if len(candidates) != 1:
        return None
    medium = candidates[0]
    tracks = medium.get("tracks") or []
    if not tracks or (ntracks and len(tracks) != ntracks):
        return None
    return (0 if exact else 1, medium)


def mb_lookup(ntracks=None):
    """Look the current disc up on MusicBrainz.

    Returns (meta_or_None, err_or_None). The meta dict matches the CDDB shape
    {album, artist, year, tracks: [str]} plus src='musicbrainz'. A miss returns
    (None, None) so the caller can fall back.
    """
    mbid, err = mb_discid_from_device()
    if err or not mbid:
        return None, err or "no DiscID"
    try:
        data = _mb_get("%s/discid/%s?inc=%s&fmt=json"
                       % (MB_API, urllib.parse.quote(mbid), MB_INC))
    except urllib.error.HTTPError as he:
        # 404/503 mean "no such TOC" or rate limiting; treat both as a miss
        if he.code in (404, 503, 400):
            return None, None
        return None, "MusicBrainz request failed: HTTP %s" % he.code
    except Exception as e:
        return None, "MusicBrainz request failed: %s" % e
    releases = data.get("releases") or []
    if not releases:
        return None, None          # not in the database
    candidates = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        selected = _mb_medium(release, mbid, ntracks)
        if selected:
            quality, medium = selected
            is_cd = (medium.get("format") or "").upper() == "CD"
            candidates.append((quality, 0 if is_cd else 1, release, medium))
    if not candidates:
        return None, None
    _, _, rel, medium = min(candidates, key=lambda item: item[:2])
    artist = _artist_string(rel.get("artist-credit"))
    album = rel.get("title") or None
    date = rel.get("date") or ""
    year = date[:4] if date else None
    titles = []
    for t in medium["tracks"]:
        titles.append(t.get("title") or (t.get("recording") or {}).get("title") or "")
    if not titles:
        return None, None
    meta = {"album": album, "artist": artist, "year": year,
            "tracks": titles, "src": "musicbrainz"}
    return meta, None


# ---------- Unified entry point ----------

def resolve(discid, fields, force=False):
    """Resolve metadata: cache first, then CD-Text, MusicBrainz and CDDB.

    force=True bypasses the cache and re-queries, which backs the manual
    "re-identify" action. Returns (meta_or_None, err_or_None).
    """
    path = _cache_path(discid)
    if not force and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                c = json.load(f)
            if not c.get("err") or time.time() - c.get("ts", 0) < CACHE_TTL:
                return c.get("meta"), c.get("err")
        except Exception:
            pass

    ntracks = int(fields[0]) if fields and fields[0].isdigit() else None

    src = None
    # 1) CD-Text: stored on the disc and the most trustworthy; accept any
    # track list it provides (year may be missing)
    meta, err = cdtext_lookup()
    src = "cdtext"
    if not (meta and meta.get("tracks")):
        # 2) MusicBrainz
        meta, err = mb_lookup(ntracks)
        src = "musicbrainz"
        if not (meta and meta.get("tracks")):
            # 3) freedb/CDDB fallback
            meta = freedb_lookup(discid, fields)
            src = "cddb"
            err = meta.get("err") if isinstance(meta, dict) and meta.get("err") else None
            if isinstance(meta, dict) and not err:
                meta.pop("err", None)
    if isinstance(meta, dict) and meta.get("tracks"):
        meta["src"] = src
        meta.setdefault("year", None)
    rec = {"ts": time.time(), "discid": discid,
           "meta": None if err else meta, "err": err}
    try:
        atomic_json(path, rec)
    except OSError:
        logger.exception("Could not persist disc metadata")
    return rec["meta"], rec["err"]


def get_meta():
    """Convenience wrapper around :func:`resolve`."""
    discid, fields, err = disc_id()
    if err:
        return None, err
    return resolve(discid, fields)

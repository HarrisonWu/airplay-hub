# Architecture

## Shape

One Python process serves a single HTML page and a small JSON API, and owns no
state of its own beyond the disc metadata cache. Everything else it does is a
supervised call into a system service — MPD, PulseAudio, BlueZ, shairport-sync —
or a short-lived child process (`cdparanoia`, `flac`, `cdrdao`, `pactl`,
`bluetoothctl`).

```
                    ┌──────────────── airplay_hub (python3 -m airplay_hub) ─────────────┐
 browser ──HTTP──▶  │  threads: HTTP handler pool │ RefreshWorker │ AirPlay pipe worker │
                    └───────┬──────────────────────────────┬──────────────────────────┘
                            │                              │
                   MPD ◀────┤                              ├─▶ /tmp/shairport-sync-metadata
                   PulseAudio ◀┤                            │
                   BlueZ ◀───┤                              │
                   /dev/sr0 ◀─┴─ cdparanoia / flac / cdrdao  │
```

## Modules

| Module | Responsibility |
| --- | --- |
| `__main__.py` | `python -m airplay_hub` entry point. |
| `webapp.py` | Routes, actions, playback policy, Bluetooth and AirPlay views. The only module that knows about the hardware. |
| `http_contract.py` | Request body limits, `Content-Length` and `Origin` enforcement, per-action argument validation. No hardware knowledge. |
| `http_server.py` | `ThreadingHTTPServer` subclass that bounds concurrent work with a semaphore and answers `503` instead of queueing without limit. |
| `optical.py` | `OpticalDrive`: the single owner of CD reads. Every read, metadata probe and rip claims an unforgeable token first. |
| `processes.py` | Bounded command output (`run_until_quiet`) and deterministic child cleanup (`terminate_and_wait`). |
| `mpd_client.py` | Minimal MPD command-list transport over a socket. No playback policy, no connection reuse across calls. |
| `background.py` | `RefreshWorker`: coalesces repeated Bluetooth status refreshes, preserving a pending full refresh. |
| `storage.py` | Filesystem boundaries: library-relative path validation, atomic JSON writes. |
| `cdmeta.py` | Disc identification and the three-tier metadata lookup. |
| `templates/index.html` | The entire UI: one document, no build step, no dependencies. |

The dependency direction is one-way. `http_contract`, `processes`, `storage` and
`background` import nothing from the application; `cdmeta`, `optical` and
`mpd_client` depend only on those; `webapp` depends on everything.

## Request path

```
GET  /                → the UI document
GET  /api/toc         → drive contents, with per-disc metadata merged in
GET  /api/status      → MPD state, AirPlay state, audio link, Bluetooth state
GET  /api/library     → albums on disk
GET  /api/shelf       → disc in the drive merged with the albums on disk
GET  /api/ripstatus   → ripping progress
GET  /api/airplay_art → cover art from the metadata pipe, else the disk cache
POST /api/action      → every mutation; {"action": ..., ...} → {"rc": 0|1, "msg": ...}
```

`POST /api/action` is the only mutating endpoint. It rejects `Transfer-Encoding`,
requires a numeric `Content-Length` under 64 KiB, rejects cross-origin requests
by `Sec-Fetch-Site` and `Origin`/`Host`, and validates arguments per action
before dispatch. Actions are `play_track`, `play_at`, `play_local`,
`play_album`, `play_cd_disc`, `prev`, `next`, `play`, `pause`, `toggle`, `stop`,
`seek`, `volume`, `mode`, `rip`, `switch_output`, `bt_power`, `bt_scan`,
`bt_pair`, `bt_connect`, `bt_disconnect` and `reidentify`.

## Concurrency

Four independent things run at once: the HTTP handler pool, the Bluetooth
refresh worker (coalescing), the AirPlay metadata pipe worker, and at most one
rip. They meet at three places, all of them explicit:

- **The optical drive.** `OpticalDrive.claim(operation)` returns a token or
  `None`, and `release(token)` only accepts the token it issued. A rip holds the
  claim for its whole duration, so a second rip, a metadata probe or a track
  read is refused with a human-readable reason rather than queued. Nested claims
  on the same thread are depth-counted, which is what lets a full `toc` read
  call the metadata path while already holding the drive.
- **The MPD queue.** A lock serialises multi-command sequences, because
  `clear` + `add`×N + `play` is not atomic from the server's point of view.
- **The AirPlay pipe.** A lock guards the decoded state dict; the HTTP threads
  copy what they need and read the FIFO only through the worker.

HTTP work is bounded by `PlayerHTTPServer`: a `BoundedSemaphore` of 16 slots,
and a request that arrives with no slot available gets one header drained and a
`503` rather than another thread. This keeps a slow hardware call from turning
into unbounded thread growth on a small board.

## AirPlay progress

shairport-sync reports the sender's timeline on the metadata pipe as `prgr`
packets holding three RTP sample counts — `start`, `current` and `end` — at
44 100 Hz. That timeline leads the audio you actually hear by the latency of the
whole output chain, so the reported position is corrected down:

```
elapsed  = max(0, (current - start) / 44100 - AIRPLAY_HUB_AP_LATENCY)
duration = max(0, (end - start) / 44100)
```

The default correction is `0.05` s. Raising it makes the progress bar lag the
audio; leaving it at `0` makes the bar lead by roughly the PulseAudio plus DAC
latency. Between `prgr` packets the elapsed time is extrapolated from the
wall-clock timestamp of the last packet, and re-anchored on `pbeg`, `pend`,
`pfls` and `prsm`. Cover art arrives as base64 `PICT` packets and is cached on
disk, so art survives a sender that stops sending it.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `AIRPLAY_HUB_AP_LATENCY` | `0.05` | Seconds subtracted from the AirPlay progress estimate. |
| `AIRPLAY_HUB_DATA_DIR` | `/opt/cdplayer` | Parent of the `meta/` metadata cache. |

Paths that are not configurable are constants at the top of `webapp.py` and
`cdmeta.py`: the optical drive (`/dev/sr0`), the MPD library
(`/var/lib/mpd/music`), the wired sink name (`output_wired`), the AirPlay
metadata pipe (`/tmp/shairport-sync-metadata`) and the listen address
(`0.0.0.0:8080`, no authentication — see [DECISIONS](DECISIONS.md)).

## Operational notes

- The process is a foreground server. Its unit sets `WorkingDirectory` to the
  deployment root so the package is importable, and runs it as the user that
  owns the audio hardware.
- Logs go to the journal. `log_message` on the handler is deliberately silent so
  per-request noise stays out of it; action failures are logged with a traceback
  and answered with a generic message.
- The metadata cache is disposable. Deleting `$AIRPLAY_HUB_DATA_DIR/meta` costs
  one upstream lookup per disc.
- Nothing in the process writes to the music library except a rip.

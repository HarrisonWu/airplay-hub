# airplay-hub

A self-hosted web console for a single-board audio hub. One page drives AirPlay
reception, local library playback, CD ripping and Bluetooth output, and the
whole thing runs on the Python standard library alone — no framework, no
database, no external services.

```
AirPlay  (shairport-sync) ┐
music library  (MPD)      ├─→ MPD / shairport-sync ─→ PulseAudio ─→ USB DAC
optical drive (cdparanoia)┘                                        └→ Bluetooth A2DP
```

## Features

- **AirPlay monitoring** — transport state, cover art and progress for whatever
  is being streamed in, with the sender's progress timeline corrected for the
  real output latency so the position matches what you hear.
- **Local library** — one merged view of the disc in the drive and the albums on
  disk. A track that has already been ripped plays from the local file, and the
  play queue spans the whole library rather than a single album.
- **Disc ripping** — one-shot ripping to tagged FLAC with per-track progress,
  visible failures and a rescan of the library when it finishes.
- **Metadata lookup** — CD-Text first, then MusicBrainz, then freedb/CDDB,
  cached per disc ID and revocable from the UI with "re-identify".
- **Bluetooth** — adapter power, discovery, pairing, connection and
  disconnection, with the result of each action reported in the page.
- **Output switching** — move playback between the wired DAC and a paired
  speaker in one action, streams included.

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map, the
request path and the concurrency model. Design rationale for the less obvious
choices is collected in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Requirements

Runtime, on the device:

| Component | Purpose |
| --- | --- |
| Python 3.10+ | the application itself; standard library only |
| MPD | playback engine, with the music library at `/var/lib/mpd/music` |
| `mpc` | MPD command client, used for transport control |
| PulseAudio | audio hub; the wired DAC and Bluetooth sinks both live here |
| `pactl` | sink inspection and output switching (`pulseaudio-utils`) |
| shairport-sync | AirPlay receiver, with the metadata pipe enabled |
| `cdparanoia`, `flac`, `cd-discid`, `cdrdao` | ripping and metadata |
| BlueZ (`bluetoothctl`) | Bluetooth adapter management over D-Bus |

Development only: Node.js 18+ for the frontend test suite.

## Install

```sh
git clone https://github.com/HarrisonWu/airplay-hub.git
cd airplay-hub
sudo install -d /opt/cdplayer
sudo cp -r src/airplay_hub /opt/cdplayer/
sudo cp deploy/airplay-hub.service /etc/systemd/system/
sudo systemctl enable --now airplay-hub.service
```

The service expects a `WorkingDirectory` of `/opt/cdplayer` and starts the
package with `python3 -m airplay_hub`. If you already run the older flat layout,
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) covers the migration and the
hash-checked deployment script.

The UI is then served at `http://<host>:8080/`.

## Configuration

Everything is optional; the defaults match a stock Orange Pi install.

| Variable | Default | Effect |
| --- | --- | --- |
| `AIRPLAY_HUB_AP_LATENCY` | `0.05` | Seconds of AirPlay progress correction. |
| `AIRPLAY_HUB_DATA_DIR` | `/opt/cdplayer` | Where the metadata cache lives. |

## Development

```sh
python3 scripts/check.py              # syntax, unit tests and frontend tests
python3 scripts/check.py --python-only
```

The suite never touches a drive, a music library, Bluetooth hardware or a live
MPD: HTTP runs on a random loopback port, the MPD protocol is exercised against
a fake peer, the library is a temporary directory and hardware subprocesses are
mocked.

## Layout

```
src/airplay_hub/     application package (see docs/ARCHITECTURE.md)
tests/               unit and frontend suites
scripts/             verification and hash-checked deployment
deploy/              systemd unit
docs/                architecture, deployment and design decisions
```

## License

[MIT](LICENSE)

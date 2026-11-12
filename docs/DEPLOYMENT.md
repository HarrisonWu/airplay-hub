# Deployment

The application deploys as a package directory plus a systemd unit. There is no
build step, no virtualenv and no third-party dependency — only the device's
`python3` and the system services listed in the [README](../README.md).

| Path | Contents |
| --- | --- |
| `/opt/cdplayer/airplay_hub/` | the application package |
| `/etc/systemd/system/airplay-hub.service` | the unit |
| `/var/lib/mpd/music` | the music library, owned by MPD |
| `$AIRPLAY_HUB_DATA_DIR/meta` | the metadata cache, disposable |

`/opt/cdplayer` must be writable by the account in the unit's `User=`, which
also owns the metadata cache.

## Fresh install

```sh
git clone https://github.com/HarrisonWu/airplay-hub.git
cd airplay-hub

sudo install -d -o 1000 -g audio /opt/cdplayer
sudo install -d -o 1000 -g audio /opt/cdplayer/meta
sudo cp -r src/airplay_hub /opt/cdplayer/
sudo chown -R 1000:audio /opt/cdplayer/airplay_hub

sudo cp deploy/airplay-hub.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airplay-hub.service
```

The unit sets `WorkingDirectory=/opt/cdplayer`, which is what makes
`python3 -m airplay_hub` resolve the package. Confirm it came up:

```sh
systemctl status airplay-hub.service
curl -s http://127.0.0.1:8080/api/status
```

`/api/status` answering with JSON means the process is serving; the UI is at
`http://<host>:8080/`.

## AirPlay 2 receiver

The distribution package of shairport-sync (4.x, e.g. `4.3.7-1` on Debian) is
an AirPlay 1 receiver. AirPlay 2 needs a locally built 5.x binary plus `nqptp`,
which supplies the PTP clock the stream is timed against. The hub itself only
reads the metadata pipe, but it reports no AirPlay session at all if the
receiver is not running.

A build verified on the Raspberry Pi 5 (Debian 13, shairport-sync `5.6.1`):

```sh
./configure --prefix=/usr/local --sysconfdir=/usr/local/etc \
  --with-airplay-2 --with-alsa --with-pulseaudio --with-soxr \
  --with-avahi --with-ssl=openssl --with-metadata \
  --with-dbus-interface --with-mpris-interface
make && sudo make install
```

| Item | Value |
| --- | --- |
| Binary | `/usr/local/bin/shairport-sync` (not the packaged `/usr/bin` one) |
| Config | `/usr/local/etc/shairport-sync.conf` — `--sysconfdir` moves it out of `/etc` |
| Timing | `nqptp.service` must be active before `shairport-sync.service` |
| Unit | `ExecStart=/usr/local/bin/shairport-sync -c /usr/local/etc/shairport-sync.conf` |
| Audio account | a drop-in overriding `User=`/`Group=` to the account owning the audio server socket (see below) |
| Metadata | `metadata.enabled = "yes"`, `pipe_name = "/tmp/shairport-sync-metadata"` |
| Output | `general.output_backend = "pulseaudio"` |

The `prgr` timeline is still expressed in 44 100 Hz RTP frames under AirPlay 2;
that is the clock base for progress arithmetic, **not** the DAC output rate. The
output format is negotiated by PulseAudio/PipeWire with the sink.

By default PipeWire fixes the graph at 48 kHz and resamples everything to it. To
let CD/AirPlay (44.1 kHz) and high-resolution files (96 kHz) reach the DAC at
their native rate, enable the rate list shipped with PipeWire 1.4.2 for the
account that owns the audio server:

```sh
mkdir -p ~/.config/pipewire/pipewire.conf.d
cp /usr/share/pipewire/pipewire.conf.avail/10-rates.conf \
   ~/.config/pipewire/pipewire.conf.d/10-rates.conf
systemctl --user restart pipewire pipewire-pulse wireplumber
sudo systemctl restart mpd shairport-sync airplay-hub
```

`10-rates.conf` sets `default.clock.allowed-rates = [ 44100 48000 88200 96000 ]`
while leaving `default.clock.rate = 48000` as the idle rate, so the graph follows
the active stream instead of upsampling 44.1 kHz material to 96 kHz. Verified on
the Raspberry Pi 5 with a MOONDROP Dawn Pro (USB Audio Class, 44.1–384 kHz,
S16/S24/S32): a 96 kHz stream opens the sink at `s32le 2ch 96000Hz`, a 44.1 kHz
stream at `s32le 2ch 44100Hz`, and the idle sink stays at 48000 Hz. Check with
`pactl list sinks | grep -E 'State:|Sample Specification:'`.

AirPlay 2 itself streams ALAC at 44.1 kHz / 16-bit; the 96 kHz path benefits
local high-resolution files played through MPD, not AirPlay.

```sh
/usr/local/bin/shairport-sync --version
systemctl is-active nqptp shairport-sync airplay-hub
```

## The audio server account

This is the one thing that reliably goes wrong on a fresh install, so it is
worth reading before the first `systemctl start`.

MPD, shairport-sync and this service all play into one audio graph, and a PipeWire
or PulseAudio server is **per user**: it publishes its socket at
`/run/user/<uid>/pulse/native` with mode 0700, so only the account that owns the
server can open it. Three settings therefore have to name the same account and
have to be changed together:

| Setting | Value |
| --- | --- |
| `User=` | the account that owns the audio server — normally the human account, not a system account; on a stock Raspberry Pi OS image the account created during imaging is uid 1000 |
| `Group=` | a group that account belongs to — `audio` exists on every audio-capable Linux |
| `Environment=XDG_RUNTIME_DIR=` | `/run/user/<uid of User=>`, written **numerically** |

Two systemd behaviours make this easy to get wrong. Both were verified against
the systemd on Raspberry Pi OS:

- **`XDG_RUNTIME_DIR=/run/user/%U` does not work.** For a system unit, `%U`
  resolves against the service manager's own user rather than against `User=`, so
  the value arrives as `/run/user/0`. Nothing warns; `pactl` simply cannot
  connect. This is why the unit spells the UID out.
- **`ProtectHome=yes` breaks it as well.** That setting replaces `/home`, `/root`
  and `/run/user` with empty inaccessible directories, so the audio socket
  disappears even when `XDG_RUNTIME_DIR` is correct. `ProtectHome=read-only` is
  the strongest setting that still leaves `/run/user` alone, and
  `ReadWritePaths=` does **not** undo `ProtectHome=yes`.

`XDG_RUNTIME_DIR` cannot simply be omitted: libpulse does not fall back to
`/run/user/<uid>` when it is unset.

When either is wrong, every output switch answers `no wired output is available`
whatever the real cause. The application probes the audio server once at startup
and logs the underlying reason, so check there first:

```sh
journalctl -u airplay-hub.service | grep -i 'audio server'
```

MPD's own account is a separate question: `mpd.conf` has a `user` directive that
dictates which account the MPD process drops to, and MPD reaches the audio server
as that account. Set it to the same account as `User=` above.

## Upgrade

Upgrades go through `scripts/deploy.py`, which is dry-run unless `--apply` is
given. It refuses to run against a live tree that has drifted from the manifest
it is given, so the first step is to record what is live now.

```sh
# 1. Baseline: the current hash of every application file, or null if absent.
sudo python3 -c "
import json, pathlib, sys
sys.path.insert(0, '.')
from scripts import deploy
target = pathlib.Path(deploy.TARGET)
print(json.dumps({n: (deploy.digest(target / n) if (target / n).exists() else None)
                  for n in deploy.FILES}, indent=2))
" | sudo tee baseline.json

# 2. Rehearse. Nothing is written.
sudo python3 scripts/deploy.py --baseline baseline.json \
     --backup "/var/backups/airplay-hub/$(date +%F-%H%M%S)"

# 3. Apply, reusing the same fresh backup path.
sudo python3 scripts/deploy.py --baseline baseline.json \
     --backup "/var/backups/airplay-hub/$(date +%F-%H%M%S)" --apply
```

`--source` defaults to the `src/` directory of the checkout, so no path argument
is normally needed.

An apply, in order: verify the baseline, parse every `.py` in the bundle, verify
the device is idle (no rip, no playback, no AirPlay stream, no Bluetooth
operation, no `cdparanoia`/`flac`/`cdrdao` process), snapshot the metadata,
music and service configuration, save the current source with its ownership and
modes, stop the service, re-verify the baseline, install atomically, import the
package as the service user, start the service, poll `http://127.0.0.1:8080/`
for a readiness response, and confirm the data snapshot is unchanged.

Any failure after the service was stopped restores the backup and starts the
service again. The failure is reported with the reason the check refused.

## Rollback

Failure during apply rolls back on its own. To roll back a deployment that
succeeded, restore from the backup that deployment created:

```sh
sudo systemctl stop airplay-hub.service
sudo python3 -c "
import pathlib, sys
sys.path.insert(0, '.')
from scripts import deploy
deploy.restore(pathlib.Path('/opt/cdplayer'), pathlib.Path('/var/backups/airplay-hub/<stamp>'))
"
sudo systemctl start airplay-hub.service
```

`restore` verifies each backed-up file against the hash recorded when the backup
was taken, and reapplies the recorded ownership and mode, before writing
anything.

## Migrating from the flat layout

Earlier revisions installed the modules directly into `/opt/cdplayer`
(`/opt/cdplayer/webapp.py`, `/opt/cdplayer/cdmeta.py`, ...) and started the
entry point as a script. The application is now a package under
`/opt/cdplayer/airplay_hub/`, so a migration is a unit change plus a copy.

1. Install the unit from `deploy/airplay-hub.service`, which runs
   `python3 -m airplay_hub` with `WorkingDirectory=/opt/cdplayer`.
2. Run the baseline procedure above as a dry run. The old flat files are not in
   `deploy.FILES`, so the baseline records the new package paths as `null` and
   the run installs cleanly.
3. Apply, and verify the UI and `/api/status` after the restart.
4. Only then remove the superseded top-level modules:

   ```sh
   sudo rm -f /opt/cdplayer/{webapp,cdmeta,mpd_client,storage,http_contract,http_server,processes,background,optical}.py
   ```

   Do this after verification, not before — the flat files are not part of the
   new baseline, so they are neither backed up nor restored.

`deploy.py` deliberately does not manage `/etc/mpd.conf`, the shairport-sync
configs or the music library; those stay under the operator's control, and the
script only proves it left them alone. It snapshots both
`/etc/shairport-sync.conf` (packaged 4.x) and
`/usr/local/etc/shairport-sync.conf` (AirPlay 2 build), since either may be the
active file depending on how the receiver was installed.

## Notes

- **No authentication.** The UI and the API are unauthenticated by design,
  because the API mutates playback and the device is expected to sit on a
  trusted LAN. Do not expose port 8080 to an untrusted network. If you must
  reach it remotely, put it behind a reverse proxy that terminates
  authentication, or behind a VPN.
- **Bluetooth and AirPlay 2 are prerequisites, not options.** The unit assumes
  BlueZ, `nqptp` and shairport-sync are already running; it does not start them.
  shairport-sync must be configured to write its metadata pipe to
  `/tmp/shairport-sync-metadata` — see [AirPlay 2 receiver](#airplay-2-receiver).
- **The metadata cache is disposable.** Deleting
  `$AIRPLAY_HUB_DATA_DIR/meta` costs one upstream lookup per disc and changes
  nothing else. "Re-identify" in the UI does the same for the disc in the drive.

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

## Fresh install

```sh
git clone https://github.com/HarrisonWu/airplay-hub.git
cd airplay-hub

sudo install -d -o mpd -g audio /opt/cdplayer
sudo install -d -o mpd -g audio /opt/cdplayer/meta
sudo cp -r src/airplay_hub /opt/cdplayer/
sudo chown -R mpd:audio /opt/cdplayer/airplay_hub

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

`deploy.py` deliberately does not manage `/etc/mpd.conf`,
`/etc/shairport-sync.conf` or the music library; those stay under the
operator's control, and the script only proves it left them alone.

## Notes

- **No authentication.** The UI and the API are unauthenticated by design,
  because the API mutates playback and the device is expected to sit on a
  trusted LAN. Do not expose port 8080 to an untrusted network. If you must
  reach it remotely, put it behind a reverse proxy that terminates
  authentication, or behind a VPN.
- **Bluetooth and AirPlay are prerequisites, not options.** The unit assumes
  BlueZ and shairport-sync are already running; it does not start them.
  shairport-sync must be configured to write its metadata pipe to
  `/tmp/shairport-sync-metadata`.
- **The metadata cache is disposable.** Deleting
  `$AIRPLAY_HUB_DATA_DIR/meta` costs one upstream lookup per disc and changes
  nothing else. "Re-identify" in the UI does the same for the disc in the drive.

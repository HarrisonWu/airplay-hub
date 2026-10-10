# Decisions

Rationale for the choices that a reader would otherwise be tempted to "fix".
Each entry states the decision, the forces behind it, and what it costs.

## 1. No container

The application is not packaged as a Docker image, and neither are the services
it drives.

A container would have to receive `/dev/sr0` and `/dev/snd` as statically bound
device nodes, reach PulseAudio's socket (which lives in a per-user runtime
directory and is not a network service by default), hold `CAP_NET_ADMIN` and a
shared network namespace for the shairport-sync and nqptp timing sockets, and
run privileged enough for BlueZ over D-Bus. The result is a container with the
isolation of a `chroot`, a second copy of the audio stack to keep in sync with
the host's, and a new class of bug — a USB DAC that re-enumerates gets a new
device node that the bind mount no longer covers. On a single-board computer
whose entire purpose is this one appliance, that trade is negative.

The upgrade path in [DEPLOYMENT](DEPLOYMENT.md) instead replaces the application
files under a hash-checked baseline with a backup and an automatic rollback.

*Cost:* no image immutability, so the deploy script has to verify what it is
about to replace.

## 2. Standard library only, no web framework

The whole UI is one HTML document served from memory; the API is six `GET`
endpoints and one `POST`. Flask or FastAPI would add a dependency set to
install, pin and audit on the device, for routing and JSON handling that is a
few dozen lines here.

The parts that a framework genuinely gives you are implemented explicitly
instead: bounded concurrency in `http_server.py`, request validation in
`http_contract.py`, atomic writes in `storage.py`.

*Cost:* the hand-rolled server has to be treated as security-relevant code. It
is written to be small enough to review in one sitting, and the request path
rejects anything it does not positively recognise.

## 3. One claim on the optical drive, not a global lock

`OpticalDrive.claim()` hands out an unforgeable token and refuses a second
claim while one is held, rather than serialising callers behind a mutex.

A lock would queue a rip behind a track read and produce a request that
eventually succeeds. Refusing produces a request that fails immediately with a
reason the UI can show. On this device only one operation can physically be
correct anyway — a read and a rip cannot interleave — so waiting is not progress.
Claims are depth-counted per thread, which lets the metadata path run inside a
TOC read without deadlocking against itself.

*Cost:* callers must handle `None` and release in a `finally`.

## 4. Bounded upstream reads

Every response read from an external service is bounded before it is parsed:
JSON bodies from MusicBrainz, CDDB gateway replies, and command output in
`processes.run_until_quiet`. A malicious or broken upstream cannot make the
process allocate without limit, and a child process that never stops talking
cannot fill memory.

*Cost:* a legitimately oversized response is rejected rather than truncated.

## 5. Bluetooth success is a terminal state, not command output

`bluetoothctl` prints success and failure in prose that varies by version, so no
code parses it. Instead `bt_connect`-style actions wait for an observable
terminal state: the `Connected` flag in `bluetoothctl info`, and a PulseAudio
sink that reports the device as connected. Only then is the action reported as
successful.

This is the difference between "the command returned 0" and "the speaker is
actually playing". Parsing output was the alternative and it broke on upgrade.

*Cost:* actions take as long as hardware takes, so they run on worker threads
and report through the refresh worker rather than blocking a request.

## 6. AirPlay progress is corrected for output latency

The sender's progress timeline describes the stream, not the sound. The
correction constant is exposed as `AIRPLAY_HUB_AP_LATENCY` (default `0.05` s)
rather than hard-coded, because it depends on the sink: a Bluetooth A2DP path
adds substantially more latency than a wired DAC, and the right value has to be
measurable on the device it runs on.

*Cost:* one number that has to be tuned per output path.

## 7. Deployment is hash-checked, not overwrite-in-place

`scripts/deploy.py` is dry-run by default. `--apply` requires a baseline
manifest covering every application file, refuses to run if any live file has
changed since that baseline was taken, refuses if a symlink is in the way,
refuses while ripping, playback or a Bluetooth operation is in flight, saves a
backup with the original ownership and modes, stops the service, re-verifies,
installs, restarts, waits for an HTTP readiness check, and rolls back on any
failure. It never touches music, metadata or service configuration, and it
compares a manifest of that data before and after to prove it did not.

The point is that a failed upgrade must not need a console. Every refusal is a
deliberate stop rather than a best effort.

*Cost:* an operator has to take a baseline and accept that in-place edits
outside the process will block the next deploy until they are reconciled.

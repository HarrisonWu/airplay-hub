"""Deploy this appliance's application bundle with drift checks and rollback.

Dry-run is the default. --apply requires an independently reviewed live hash
manifest and a new backup directory. Never manages music, metadata or services'
configuration. Run with the target's Python, as root for an actual deployment.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

FILES = tuple('airplay_hub/' + name for name in (
    'webapp.py', 'cdmeta.py', 'mpd_client.py', 'storage.py', 'http_contract.py',
    'http_server.py', 'processes.py', 'background.py', 'optical.py',
    '__init__.py', '__main__.py', 'templates/index.html'))
TARGET = Path('/opt/cdplayer')
SERVICE = 'airplay-hub.service'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_baseline(target, baseline):
    if set(baseline) != set(FILES):
        raise ValueError('Baseline must cover every application file, including null for absent files')
    for name, expected in baseline.items():
        path = target / name
        if path.is_symlink():
            raise ValueError('Refusing symlink: ' + name)
        actual = digest(path) if path.exists() else None
        if actual != expected:
            raise ValueError('Production source changed: ' + name)


def write_atomic(destination, data, attributes):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.deploy-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, attributes['uid'], attributes['gid'])
        os.chmod(temporary, attributes['mode'])
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_backup(target, backup, baseline):
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    attributes = {}
    for name, expected in baseline.items():
        if expected is None:
            continue
        original, saved = target / name, backup / name
        info = original.stat()
        attributes[name] = {'uid': info.st_uid, 'gid': info.st_gid, 'mode': info.st_mode & 0o777}
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, saved)
        if digest(saved) != expected:
            raise ValueError('Source changed while backing up: ' + name)
    (backup / 'baseline.json').write_text(json.dumps(baseline, indent=2))
    (backup / 'attributes.json').write_text(json.dumps(attributes, indent=2))
    return attributes


def restore(target, backup):
    attributes = json.loads((backup / 'attributes.json').read_text())
    baseline = json.loads((backup / 'baseline.json').read_text())
    for name in attributes:
        if digest(backup / name) != baseline[name]:
            raise ValueError('Backup damaged: ' + name)
    for name, info in attributes.items():
        write_atomic(target / name, (backup / name).read_bytes(), info)
    # Added helper modules remain unused after restoring the previous entrypoint.
    # Avoid deleting files that may have been changed since deployment.


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=40).stdout


def get_json(path):
    with urllib.request.urlopen('http://127.0.0.1:8080' + path, timeout=15) as response:
        return json.load(response)


def data_manifest():
    return {
        'metadata': {str(p): digest(p) for p in sorted((TARGET / 'meta').glob('*.json'))},
        'music': [(str(p), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in sorted(Path('/var/lib/mpd/music').rglob('*')) if p.is_file()],
        'configuration': {str(p): digest(p) for p in
                          (Path('/etc/shairport-sync.conf'), Path('/etc/mpd.conf')) if p.is_file()},
    }


def check_idle():
    if get_json('/api/ripstatus')['running']:
        raise RuntimeError('Ripping active')
    status = get_json('/api/status')
    if status['state'] != 'stopped' or status['airplay']['active']:
        raise RuntimeError('Playback active; deployment deferred')
    bt = status.get('bt') or {}
    if bt.get('busy') or bt.get('discovering'):
        raise RuntimeError('Bluetooth operation active')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / 'comm').read_text().strip() in ('cdparanoia', 'flac', 'cdrdao'):
                raise RuntimeError('Optical/encoding process active: ' + proc.name)
        except (FileNotFoundError, PermissionError):
            pass


def deploy(source, backup, baseline, apply=False):
    check_baseline(TARGET, baseline)
    bundle = {name: (source / name).read_bytes() for name in FILES}
    for name, data in bundle.items():
        if name.endswith('.py'):
            ast.parse(data.decode('utf-8'), filename=name)
    check_idle()
    if not apply:
        return {'dry_run': True, 'files': list(FILES), 'baseline_matches': True, 'idle': True}
    import pwd
    import grp
    new_attributes = {'uid': pwd.getpwnam('mpd').pw_uid,
                      'gid': grp.getgrnam('audio').gr_gid, 'mode': 0o644}
    before = data_manifest()
    attributes = save_backup(TARGET, backup, baseline)
    (backup / 'data-before.json').write_text(json.dumps(before, ensure_ascii=False))
    (backup / 'service-before.txt').write_text(run('systemctl', 'cat', SERVICE))
    check_baseline(TARGET, baseline)
    check_idle()
    run('systemctl', 'stop', SERVICE)
    # Recheck before the first mutation; an external change is never rolled back.
    try:
        check_baseline(TARGET, baseline)
    except BaseException:
        run('systemctl', 'start', SERVICE)
        raise
    try:
        for name, data in bundle.items():
            write_atomic(TARGET / name, data, attributes.get(name, new_attributes))
        imported = run('runuser', '-u', 'mpd', '--', '/usr/bin/python3', '-c',
                       "import sys; sys.path.insert(0, '/opt/cdplayer'); import airplay_hub.webapp; "
                       "print('service-user import OK', len(airplay_hub.webapp.PAGE), airplay_hub.webapp._AP_LAT)")
        run('systemctl', 'start', SERVICE)
        ready = False
        for _ in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2) as response:
                    ready = response.status == 200 and b'actionStatus' in response.read()
                if ready:
                    break
            except OSError:
                pass
            time.sleep(0.3)
        if not ready:
            raise RuntimeError('Readiness failed')
        if data_manifest() != before:
            raise RuntimeError('Music/metadata/configuration changed; investigate')
        installed = {name: digest(TARGET / name) for name in FILES}
        if installed != {name: hashlib.sha256(data).hexdigest() for name, data in bundle.items()}:
            raise RuntimeError('Installed source hash mismatch')
        result = {'backup': str(backup), 'installed_sha256': installed,
                  'data_manifest_unchanged': True, 'import': imported.strip(),
                  'service': run('systemctl', 'show', SERVICE, '-p', 'MainPID',
                                 '-p', 'ActiveState', '-p', 'NRestarts')}
        (backup / 'deployment-result.json').write_text(json.dumps(result, indent=2))
        return result
    except BaseException:
        run('systemctl', 'stop', SERVICE)
        restore(TARGET, backup)
        run('systemctl', 'start', SERVICE)
        print('Restored backed-up source and restarted service', flush=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(deploy(args.source, args.backup,
                           json.loads(args.baseline.read_text()), args.apply), indent=2))

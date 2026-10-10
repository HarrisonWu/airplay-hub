"""Filesystem boundaries shared by metadata and library operations."""
import json
import os
from pathlib import Path
import tempfile
import threading

_replace_lock = threading.Lock()


def library_path(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or any(c in relative for c in '\r\n\0')
            or Path(relative).is_absolute() or ':' in relative.split('/')[0]
            or '..' in relative.split('/')):
        raise ValueError('invalid music library path')
    base = Path(root).resolve()
    target = (base / relative).resolve()
    if target == base or base not in target.parents:
        raise ValueError('path must stay inside the music library')
    return target


def atomic_json(path, value):
    """Replace only a complete JSON record, using a unique same-directory file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, prefix='.metadata-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Serialize publication within this process (also required on Windows).
        with _replace_lock:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

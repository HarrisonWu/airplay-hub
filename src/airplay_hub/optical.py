"""One owner for application CD reads, metadata probing and ripping."""
import threading


class OpticalDrive:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None
        self._operation = None
        self._thread = None
        self._depth = 0

    def claim(self, operation):
        with self._lock:
            if self._owner is not None:
                # Queue construction may need a TOC before starting CD playback.
                if (self._operation == 'playback' and operation == 'toc'
                        and self._thread == threading.get_ident()):
                    self._depth += 1
                    return self._owner
                return None
            token = object()
            self._owner, self._operation = token, operation
            self._thread, self._depth = threading.get_ident(), 1
            return token

    def release(self, token):
        with self._lock:
            if self._owner is not token:
                raise RuntimeError('Optical ownership mismatch')
            self._depth -= 1
            if self._depth == 0:
                self._owner = self._operation = self._thread = None

    @property
    def operation(self):
        with self._lock:
            return self._operation

"""Coalesce repeated status refreshes while preserving a requested full refresh."""
import logging
import threading

logger = logging.getLogger(__name__)


class RefreshWorker:
    def __init__(self, light, full):
        self._light, self._full = light, full
        self._lock = threading.Lock()
        self._running = self._full_pending = False
        self.idle = threading.Event()
        self.idle.set()

    def request(self, full=False):
        with self._lock:
            if self._running:
                self._full_pending |= full
                return
            self._running = True
            self._full_pending = full
            self.idle.clear()
            try:
                threading.Thread(target=self._work, daemon=True,
                                 name='bluetooth-refresh').start()
            except BaseException:
                self._running = self._full_pending = False
                self.idle.set()
                raise

    def _work(self):
        while True:
            with self._lock:
                full = self._full_pending
                self._full_pending = False
            try:
                (self._full if full else self._light)()
            except Exception:
                logger.exception('Background refresh failed')
            with self._lock:
                if not self._full_pending:
                    self._running = False
                    self.idle.set()
                    return

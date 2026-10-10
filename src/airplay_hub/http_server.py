"""Bound concurrent HTTP work without adding a framework or task queue."""
import threading
import time
from http.server import ThreadingHTTPServer


class PlayerHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, handler, max_workers=16):
        self._slots = threading.BoundedSemaphore(max_workers)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                # Drain at most one bounded header before rejecting. Closing an
                # unread GET socket can reset it before the client receives 503.
                deadline = time.monotonic() + 0.2
                header = bytearray()
                while b'\r\n\r\n' not in header and len(header) < 65536:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    request.settimeout(remaining)
                    chunk = request.recv(min(4096, 65536 - len(header)))
                    if not chunk:
                        break
                    header.extend(chunk)
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\n'
                                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

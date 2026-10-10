"""Bounded command output and deterministic child-process cleanup on Linux."""
import os
import selectors
import subprocess
import time


def terminate_and_wait(process, grace=2):
    """Reap children even when terminate must escalate to kill."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=grace)
    else:
        process.wait()


def run_until_quiet(command, quiet=0.6, timeout=6, limit=256 * 1024):
    """Collect bytes, not lines: a partial line must never defeat the deadline.

    Quiet output is the established bluetoothctl completion heuristic. A hard
    deadline or output overflow is failure; natural exit retains its return code.
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=0)
    output = bytearray()
    started = last_output = time.monotonic()
    code = -1
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                now = time.monotonic()
                remaining = timeout - (now - started)
                if remaining <= 0:
                    break
                if output and now - last_output >= quiet:
                    code = process.poll()
                    code = 0 if code is None else code
                    break
                wait = min(remaining, 0.1)
                if output:
                    wait = min(wait, max(0, quiet - (now - last_output)))
                if not selector.select(wait):
                    continue
                chunk = os.read(process.stdout.fileno(), min(4096, limit + 1 - len(output)))
                if not chunk:
                    # EOF is not process exit: it may have closed stdout early.
                    remaining = timeout - (time.monotonic() - started)
                    try:
                        code = process.wait(timeout=max(0, remaining))
                    except subprocess.TimeoutExpired:
                        code = -1
                    break
                output.extend(chunk)
                last_output = time.monotonic()
                if len(output) > limit:
                    code = -2
                    break
    finally:
        terminate_and_wait(process)
        process.stdout.close()
    return code, bytes(output[:limit]).decode('utf-8', 'replace')

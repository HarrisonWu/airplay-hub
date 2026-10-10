"""Small MPD command-list transport; no playback policy or global connections."""
import socket
import time


def quote(value):
    if not isinstance(value, str) or any(c in value for c in '\r\n\0'):
        raise ValueError('MPD argument contains invalid characters')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def execute(commands, timeout=60, address=('127.0.0.1', 6600)):
    """Return the existing (rc, message) contract. ACK stops the remaining list.

    A command list is fail-fast, not a transaction: earlier commands may already
    have changed the queue. Never retry a partially executed playback command.
    """
    try:
        commands = list(commands)
        for command in commands:
            if (not isinstance(command, str) or not command.strip()
                    or any(c in command for c in '\r\n\0')
                    or command.split()[0].startswith('command_list_')):
                raise ValueError('invalid MPD command')
        if not commands:
            return 0, ''
        payload = ('command_list_begin\n' + '\n'.join(commands)
                   + '\ncommand_list_end\n').encode('utf-8')
        deadline = time.monotonic() + timeout
        with socket.create_connection(address, timeout=min(5, timeout)) as sock:
            with sock.makefile('rb') as stream:
                def read_line():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('MPD timed out')
                    sock.settimeout(remaining)
                    line = stream.readline(65537)
                    if not line or not line.endswith(b'\n') or len(line) > 65536:
                        raise OSError('MPD response truncated or over the limit')
                    return line

                if not read_line().startswith(b'OK MPD '):
                    return -2, 'MPD handshake failed'
                sock.sendall(payload)
                total = 0
                while True:
                    line = read_line()
                    total += len(line)
                    if total > 1024 * 1024:
                        raise OSError('MPD response over the limit')
                    if line == b'OK\n':
                        return 0, ''
                    if line.startswith(b'ACK '):
                        return 1, line.decode('utf-8', 'replace').strip()
    except (OSError, ValueError, UnicodeError) as exc:
        return -1, str(exc)

"""Bounded HTTP input and action validation, independent of audio hardware."""
import json
import re
from urllib.parse import urlsplit

MAX_BODY = 64 * 1024
REQUEST_TIMEOUT = 10


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_action(headers, stream):
    if headers.get('Transfer-Encoding'):
        raise RequestError('unsupported Transfer-Encoding')
    lengths = headers.get_all('Content-Length', [])
    if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
        raise RequestError('a valid Content-Length is required')
    # Bound before int(), including arbitrarily long digit strings.
    if len(lengths[0]) > 8 or int(lengths[0]) > MAX_BODY:
        raise RequestError('request body too large', 413)
    length = int(lengths[0])
    origin = headers.get('Origin')
    if headers.get('Sec-Fetch-Site') == 'cross-site':
        raise RequestError('cross-origin control requests are not allowed', 403)
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise RequestError('invalid Origin') from None
        if parsed.scheme not in ('http', 'https') or parsed.netloc != headers.get('Host'):
            raise RequestError('cross-origin control requests are not allowed', 403)
    raw = stream.read(length)
    if len(raw) != length:
        raise RequestError('incomplete request body')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RequestError('the request must be valid JSON') from None
    validate_action(data)
    return data


def validate_action(data):
    if not isinstance(data, dict) or not isinstance(data.get('action'), str):
        raise RequestError('an action string and a JSON object are required')
    action = data['action']
    numeric = {'play_track': ('track', 1), 'volume': ('volume', 50), 'seek': ('pos', None)}
    if action in numeric:
        field, default = numeric[action]
        value = data.get(field, default)
        if (isinstance(value, bool) or not isinstance(value, (str, int))
                or not re.fullmatch(r'-?[0-9]{1,10}', str(value))):
            raise RequestError(field + ' must be an integer')
        if action == 'play_track' and not 1 <= int(value) <= 99:
            raise RequestError('track numbers must be between 1 and 99')
    required = {'play_at': 'uri', 'play_local': 'file', 'play_album': 'dir',
                'bt_pair': 'mac', 'bt_connect': 'mac', 'bt_disconnect': 'mac',
                'switch_output': 'to', 'mode': 'key'}
    if action in required:
        field = required[action]
        value = data.get(field)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise RequestError(field + ' must be a non-empty string')
        if any(c in value for c in '\r\n\0'):
            raise RequestError(field + ' contains invalid characters')
    if action in ('mode', 'bt_power') and not isinstance(data.get('on'), bool):
        raise RequestError('on must be a boolean')
    if action == 'rip' and data.get('tracks') is not None:
        tracks = data['tracks']
        if (not isinstance(tracks, list) or not 1 <= len(tracks) <= 99
                or any(type(n) is not int or not 1 <= n <= 99 for n in tracks)
                or len(set(tracks)) != len(tracks)):
            raise RequestError('tracks must be a list of distinct track numbers')

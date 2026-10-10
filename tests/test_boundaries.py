import io
import json
import socket
import tempfile
import threading
import unittest
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from airplay_hub import cdmeta
from airplay_hub import mpd_client
from airplay_hub import webapp
from airplay_hub.http_contract import MAX_BODY, RequestError, read_action
from airplay_hub.http_server import PlayerHTTPServer
from airplay_hub.storage import atomic_json, library_path


class MPDTransport(unittest.TestCase):
    def exchange(self, reply, banner=b'OK MPD 0.23.5\n'):
        client, peer = socket.socketpair()
        received = []
        def daemon():
            with peer, peer.makefile('rb') as stream:
                peer.sendall(banner)
                if banner != b'OK MPD 0.23.5\n':
                    return
                while True:
                    line = stream.readline()
                    received.append(line)
                    if not line or line == b'command_list_end\n':
                        break
                if reply:
                    peer.sendall(reply)
        worker = threading.Thread(target=daemon)
        worker.start()
        with patch.object(mpd_client.socket, 'create_connection', return_value=client):
            result = mpd_client.execute(['clear', 'add "Artist/Song.flac"', 'play 0'], timeout=1)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(client.fileno(), -1)
        return result, b''.join(received)

    def test_command_list_success_with_data_lines(self):
        result, sent = self.exchange(b'updating_db: 1\nOK\n')
        self.assertEqual(result, (0, ''))
        self.assertEqual(sent, b'command_list_begin\nclear\nadd "Artist/Song.flac"\nplay 0\ncommand_list_end\n')

    def test_ack_surfaces_error_and_closes_resources(self):
        result, _ = self.exchange(b'ACK [50@1] {add} No such song\n')
        self.assertEqual(result, (1, 'ACK [50@1] {add} No such song'))

    def test_disconnect_is_error(self):
        self.assertEqual(self.exchange(b'')[0][0], -1)

    def test_invalid_banner(self):
        self.assertEqual(self.exchange(b'', banner=b'HELLO\n')[0][0], -2)

    def test_quote_escapes_backslash_and_quotes(self):
        self.assertEqual(mpd_client.quote('A\\B"C'), '"A\\\\B\\"C"')

    def test_commands_cannot_inject_newlines(self):
        with patch.object(mpd_client.socket, 'create_connection') as connect:
            for value in ('add "a"\nclear', 'x\rplay', 'x\0', 'command_list_end', ''):
                self.assertNotEqual(mpd_client.execute([value])[0], 0)
            connect.assert_not_called()

    def test_timeout_is_error_and_socket_closed(self):
        client, peer = socket.socketpair()
        with peer, patch.object(mpd_client.socket, 'create_connection', return_value=client):
            self.assertEqual(mpd_client.execute(['stop'], timeout=0.02)[0], -1)
        self.assertEqual(client.fileno(), -1)


class RequestParsing(unittest.TestCase):
    def parse(self, raw, **headers):
        message = Message()
        message['Content-Length'] = str(len(raw))
        for key, value in headers.items():
            message[key.replace('_', '-')] = value
        return read_action(message, io.BytesIO(raw))

    def test_rejects_wrong_json_shapes_and_types(self):
        for value in ([], None, 1, 'x', {}, {'action': []},
                      {'action': 'volume', 'volume': {}},
                      {'action': 'bt_pair', 'mac': 2},
                      {'action': 'bt_power', 'on': 'false'},
                      {'action': 'rip', 'tracks': [True]},
                      {'action': 'rip', 'tracks': [1, 1]}):
            with self.subTest(value=value), self.assertRaises(RequestError):
                self.parse(json.dumps(value).encode())

    def test_oversize_rejected_before_read(self):
        headers = Message()
        headers['Content-Length'] = str(MAX_BODY + 1)
        with self.assertRaises(RequestError) as error:
            read_action(headers, None)
        self.assertEqual(error.exception.status, 413)

    def test_framing_errors_rejected_before_read(self):
        for lengths in ([], ['-1'], ['abc'], ['1', '1']):
            headers = Message()
            for length in lengths:
                headers['Content-Length'] = length
            with self.subTest(lengths=lengths), self.assertRaises(RequestError):
                read_action(headers, None)

    def test_chunked_rejected(self):
        with self.assertRaises(RequestError):
            self.parse(b'{}', Transfer_Encoding='chunked')

    def test_cross_origin_rejected_same_origin_accepted(self):
        raw = b'{"action":"stop"}'
        with self.assertRaises(RequestError) as error:
            self.parse(raw, Origin='https://unrelated.invalid', Host='player:8080')
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.parse(raw, Origin='http://player:8080', Host='player:8080')['action'], 'stop')
        self.assertEqual(self.parse(raw)['action'], 'stop')

    def test_truncated_body(self):
        headers = Message()
        headers['Content-Length'] = '100'
        with self.assertRaises(RequestError):
            read_action(headers, io.BytesIO(b'{}'))

    def test_invalid_origin_is_a_client_error(self):
        with self.assertRaises(RequestError) as error:
            self.parse(b'{"action":"stop"}', Origin='http://[invalid', Host='player')
        self.assertEqual(error.exception.status, 400)


class HTTPIntegration(unittest.TestCase):
    def setUp(self):
        self.server = PlayerHTTPServer(('127.0.0.1', 0), webapp.Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, method, path, body=None):
        connection = HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_existing_get_routes(self):
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertEqual(body, webapp.PAGE.encode('utf-8'))
        for path in ('/index.html', '/api/ripstatus'):
            self.assertEqual(self.request('GET', path)[0], 200)
        self.assertEqual(self.request('GET', '/missing')[0], 404)

    def test_action_keeps_success_envelope(self):
        with patch.object(webapp, 'do_action', return_value=(0, 'playing')) as action:
            status, body = self.request('POST', '/api/action', '{"action":"play"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'rc': 0, 'msg': 'playing'})
        action.assert_called_once_with({'action': 'play'})

    def test_invalid_body_never_reaches_audio(self):
        with patch.object(webapp, 'do_action') as action:
            for body in ('[]', 'null', 'bad', '{"action":"volume","volume":{}}'):
                status, data = self.request('POST', '/api/action', body)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(data)['rc'], 1)
            action.assert_not_called()

    def test_unexpected_failure_gets_json_without_exception_details(self):
        with patch.object(webapp, 'do_action', side_effect=OSError('private path')), self.assertLogs('airplay_hub.webapp'):
            status, body = self.request('POST', '/api/action', '{"action":"stop"}')
        self.assertEqual(status, 500)
        self.assertNotIn(b'private path', body)

    def test_stalled_body_returns_timeout_without_dispatch(self):
        with patch.object(webapp, 'REQUEST_TIMEOUT', 0.03), patch.object(webapp, 'do_action') as action:
            with socket.create_connection(self.server.server_address, timeout=2) as client:
                client.sendall(b'POST /api/action HTTP/1.0\r\nContent-Length: 100\r\n\r\n')
                with client.makefile('rb') as response:
                    self.assertIn(b'408', response.readline())
                    response.read()
            action.assert_not_called()

    def test_server_bounds_concurrent_work(self):
        self.server._slots = threading.BoundedSemaphore(1)
        self.server._slots.acquire()
        try:
            self.assertEqual(self.request('GET', '/')[0], 503)
        finally:
            self.server._slots.release()
        self.assertEqual(self.request('GET', '/')[0], 200)


class Persistence(unittest.TestCase):
    def test_failed_replace_keeps_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.json'
            atomic_json(path, {'title': 'old'})
            with patch('airplay_hub.storage.os.replace', side_effect=OSError('full')):
                with self.assertRaises(OSError):
                    atomic_json(path, {'title': 'new'})
            self.assertEqual(json.loads(path.read_text()), {'title': 'old'})
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_concurrent_metadata_writes_are_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.json'
            errors = []
            def write(n):
                try:
                    atomic_json(path, {'n': n, 'title': 'Song' * 100})
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
            self.assertEqual(errors, [])
            self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['title']), 400)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_library_rejects_escape_and_allows_unicode(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('../outside', '/outside', 'C:/outside', 'a/../../x', 'a\nx', 'a\\b'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    library_path(folder, name)
            self.assertEqual(library_path(folder, 'Artist/Track.flac').name, 'Track.flac')

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            link = Path(folder) / 'escape'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Symlink creation unavailable on this host')
            with self.assertRaises(ValueError):
                library_path(folder, 'escape/secret.flac')

    def test_cache_id_cannot_escape_directory(self):
        with self.assertRaises(ValueError):
            cdmeta._cache_path('../../outside')

    def test_cddb_multiple_and_no_match(self):
        for code in ('210', '211'):
            with patch.object(cdmeta, '_gate', side_effect=[
                    code + ' matches\nrock 12345678 Artist / Album\n.\n',
                    '210 OK\nDTITLE=Artist / Album\nTTITLE0=Song\n.\n']):
                self.assertEqual(cdmeta.freedb_lookup('12345678', ['1', '150', '60'])['album'], 'Album')
        with patch.object(cdmeta, '_gate', return_value='202 No match\n'):
            self.assertIsNone(cdmeta.freedb_lookup('12345678', ['1', '150', '60']))


class RipFailures(unittest.TestCase):
    def test_initialization_exception_releases_slot(self):
        with patch.dict(webapp._rip, {'running': True, 'finished_t': 0}), patch.object(
                webapp, '_album_dirname', side_effect=ValueError('bad metadata')), self.assertLogs('airplay_hub.webapp'):
            webapp.rip_worker([(1, 'Song')], {}, '12345678')
            self.assertFalse(webapp._rip['running'])
            self.assertEqual(webapp._rip['err'], 'bad metadata')
            self.assertEqual(webapp._rip['finished_t'], 0)

    def test_failed_encoder_removes_staging_files(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(webapp, 'LIB', folder):
            def run(command, timeout):
                if command[0] == 'cdparanoia':
                    Path(command[-1]).write_bytes(b'wav')
                    return 0, ''
                Path(command[command.index('-o') + 1]).write_bytes(b'partial flac')
                return 1, 'encoder failed'
            with patch.object(webapp, 'run', side_effect=run):
                ok, message = webapp._rip_one_track(1, 1, 'Song', 'Album', {}, '12345678')
            self.assertFalse(ok)
            self.assertIn('encoder failed', message)
            self.assertEqual(list((Path(folder) / 'Album').iterdir()), [])

    def test_thread_start_failure_releases_rip_slot(self):
        from test_regressions import TOC
        with patch.dict(webapp._rip, {'running': False}), patch.object(
                webapp, 'get_toc_payload', return_value=TOC), patch.object(
                webapp.threading, 'Thread', side_effect=RuntimeError('no threads')), patch.object(
                webapp, 'mpc', return_value=(0, '')), self.assertLogs('airplay_hub.webapp'):
            self.assertFalse(webapp.start_rip(None)[0])
            self.assertFalse(webapp.rip_status()['running'])

    def test_bt_thread_start_failure_releases_slots(self):
        with patch.dict(webapp._bt_state, {'discovering': False}), patch.dict(
                webapp._bt_busy, {'mac': None}), patch.object(
                webapp.threading, 'Thread', side_effect=RuntimeError('no threads')):
            self.assertEqual(webapp.bt_scan()[0], 1)
            self.assertFalse(webapp._bt_state['discovering'])
            self.assertEqual(webapp._bt_run_busy('AA', 'pair', lambda: None, 'ok')[0], 1)
            self.assertIsNone(webapp._bt_busy['mac'])


class PlaybackIntegration(unittest.TestCase):
    def test_local_album_builds_complete_batch_before_mutation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(webapp, 'LIB', folder):
            album = Path(folder) / 'Album'
            album.mkdir()
            (album / '02.flac').touch()
            (album / '01.flac').touch()
            with patch.object(webapp, '_mpd_run', return_value=(0, '')) as execute:
                self.assertEqual(webapp.play_local(None, album=True, dir_rel='Album'), (0, ''))
            execute.assert_called_once_with(['clear', 'add "Album/01.flac"', 'add "Album/02.flac"', 'play'])

    def test_reidentify_invalidates_previous_toc(self):
        def rebuild():
            self.assertIsNone(webapp._toc['payload'])
            return {}
        with patch.dict(webapp._toc, {'t': 100, 'payload': {'old': True}}), patch.dict(
                webapp._meta_mem, {}), patch.object(webapp, 'mpc', return_value=(0, '')), patch.object(
                cdmeta, 'disc_id', return_value=('12345678', ['1', '150', '60'], None)), patch.object(
                cdmeta, 'resolve', return_value=({'tracks': ['New'], 'album': 'New'}, None)), patch.object(
                webapp, '_read_toc_payload', side_effect=rebuild) as read:
            self.assertEqual(webapp.do_action({'action': 'reidentify'})[0], 0)
            read.assert_called_once()


if __name__ == '__main__':
    unittest.main()

import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from airplay_hub import cdmeta
from airplay_hub import webapp


def packet(payload=b'', code='minm', kind='core', declared=None):
    size = len(payload) if declared is None else declared
    header = ('<item><type>%s</type><code>%s</code><length>%d</length>\n'
              % (kind.encode().hex(), code.encode().hex(), size)).encode()
    if not size:
        return header + b'</item>\n'
    return header + b'<data encoding="base64">\n' + base64.b64encode(payload) + b'\n</data></item>\n'


class AirPlayBoundaries(unittest.TestCase):
    def test_adjacent_metadata_and_empty_session_event(self):
        data = packet('Song'.encode()) + packet(code='pbeg', kind='ssnc') + packet(b'album', code='asal')
        with patch.object(webapp, '_ap_dispatch') as dispatch:
            webapp._ap_pump(io.BytesIO(data))
        self.assertEqual([c.args for c in dispatch.call_args_list],
                         [('core', 'minm', 'Song'.encode()), ('ssnc', 'pbeg', b''), ('core', 'asal', b'album')])

    def test_wrapped_base64_preserves_following_bytes(self):
        stream = io.BytesIO(b'YW\r\nJj\nZA==\n</data>')
        self.assertEqual(webapp._ap_read_b64(stream, 8), b'YWJjZA==')
        self.assertEqual(stream.read(), b'\n</data>')

    def test_oversized_payload_rejected_before_reading_body(self):
        stream = io.BytesIO(packet(b'x', declared=webapp._AP_MAX_PAYLOAD + 1))
        with patch.object(webapp, '_ap_dispatch') as dispatch, self.assertLogs('airplay_hub.webapp'):
            webapp._ap_pump(stream)
        dispatch.assert_not_called()
        self.assertTrue(stream.read().startswith(b'<data'))

    def test_invalid_base64_length_or_tag_does_not_dispatch(self):
        for content in (packet(b'abc').replace(b'YWJj', b'!!!!'), packet(b'abc', declared=2),
                        packet(b'abc').replace(b'base64', b'unknown'), packet(b'abc')[:90]):
            with self.subTest(content=content), patch.object(webapp, '_ap_dispatch') as dispatch:
                webapp._ap_pump(io.BytesIO(content))
                dispatch.assert_not_called()

    def test_header_and_whitespace_are_bounded(self):
        stream = io.BytesIO(b'x' * 10000)
        webapp._ap_pump(stream)
        self.assertEqual(stream.tell(), webapp._AP_MAX_LINE + 1)
        stream = io.BytesIO(b'\n' * 10000)
        self.assertIsNone(webapp._ap_read_b64(stream, 4))
        self.assertLess(stream.tell(), 5000)

    def test_cached_art_is_size_limited(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'image.jpg').write_bytes(b'x' * 33)
            with patch.object(webapp, '_AP_ART_CACHE_DIR', folder), patch.object(
                    webapp, '_AP_MAX_PAYLOAD', 32), patch.dict(webapp._ap_cache_memo, p='', mt=0, data=b''):
                self.assertEqual(webapp._ap_cache_art(), (b'', '', 0.0))
                self.assertEqual(webapp._ap_cache_memo['data'], b'')


class RemoteMetadataBoundaries(unittest.TestCase):
    def test_http_read_is_bounded(self):
        with patch.object(cdmeta, 'MAX_RESPONSE_BYTES', 4):
            self.assertEqual(cdmeta._bounded_read(io.BytesIO(b'abcd')), b'abcd')
            stream = io.BytesIO(b'abcdefgh')
            with self.assertRaises(ValueError): cdmeta._bounded_read(stream)
            self.assertEqual(stream.tell(), 5)

    def test_musicbrainz_requires_object(self):
        with patch.object(cdmeta.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')):
            with self.assertRaises(ValueError): cdmeta._mb_get('https://example.test/')


class MusicBrainzSelection(unittest.TestCase):
    def medium(self, titles, discid=None):
        result = {'format': 'CD', 'track-count': len(titles), 'tracks': [{'title': t} for t in titles]}
        if discid is not None: result['discs'] = [{'id': discid}]
        return result

    def lookup(self, releases, count=2):
        with patch.object(cdmeta, 'mb_discid_from_device', return_value=('current', None)), patch.object(
                cdmeta, '_mb_get', return_value={'releases': releases}) as get:
            result = cdmeta.mb_lookup(count)
        self.assertIn('discids', get.call_args.args[0])
        return result

    def test_second_disc_uses_only_matching_medium(self):
        result, error = self.lookup([{'title': 'Double album', 'media': [
            self.medium(['Wrong 1', 'Wrong 2'], 'other'), self.medium(['Right 1', 'Right 2'], 'current')]}])
        self.assertIsNone(error)
        self.assertEqual(result['tracks'], ['Right 1', 'Right 2'])

    def test_exact_disc_id_outweighs_count_only_release(self):
        result, _ = self.lookup([{'title': 'Guess', 'media': [self.medium(['a', 'b'])]},
                                {'title': 'Exact', 'media': [self.medium(['c', 'd'], 'current')]}])
        self.assertEqual(result['album'], 'Exact')

    def test_ambiguous_same_length_discs_fall_back(self):
        self.assertEqual(self.lookup([{'media': [self.medium(['a', 'b']), self.medium(['c', 'd'])]}]), (None, None))

    def test_known_different_disc_does_not_use_count_fallback(self):
        self.assertEqual(self.lookup([{'media': [self.medium(['a', 'b'], 'other')]}]), (None, None))

    def test_unique_count_match_without_ids_is_compatible(self):
        result, _ = self.lookup([{'media': [self.medium(['a']), self.medium(['b', 'c'])]}])
        self.assertEqual(result['tracks'], ['b', 'c'])

    def test_incomplete_tracklist_falls_back(self):
        self.assertEqual(self.lookup([{'media': [self.medium(['a'], 'current')]}]), (None, None))

    def test_recording_title_fallback(self):
        medium = self.medium(['', 'b'], 'current')
        medium['tracks'][0]['recording'] = {'title': 'Recording title'}
        result, _ = self.lookup([{'media': [medium]}])
        self.assertEqual(result['tracks'][0], 'Recording title')


if __name__ == '__main__': unittest.main()

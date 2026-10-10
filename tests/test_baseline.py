import copy
import unittest
from unittest.mock import patch

from airplay_hub import cdmeta
from airplay_hub import webapp


class ExistingContracts(unittest.TestCase):
    def test_page_contains_existing_endpoints(self):
        for endpoint in ('/api/action', '/api/status', '/api/shelf', '/api/ripstatus'):
            self.assertIn(endpoint, webapp.PAGE)

    def test_volume_clamps_and_accepts_slider_string(self):
        with patch.object(webapp, 'mpc', return_value=(0, '')) as mpc:
            self.assertEqual(webapp.do_action({'action': 'volume', 'volume': '120'}), (0, ''))
            mpc.assert_called_once_with('volume', '100')

    def test_unknown_action_is_application_error(self):
        self.assertEqual(webapp.do_action({'action': 'unknown'})[0], 1)

    def test_single_enables_repeat(self):
        with patch.object(webapp, 'mpc', return_value=(0, '')) as mpc:
            self.assertEqual(webapp.do_action({'action': 'mode', 'key': 'single', 'on': True})[0], 0)
            self.assertEqual([c.args for c in mpc.call_args_list], [('single', 'on'), ('repeat', 'on')])

    def test_shelf_matches_artist_alias_and_multidisc_offset(self):
        toc = {'err': None, 'artist': 'Example Alias', 'album': 'Live', 'year': '2020',
               'discid': '12345678', 'offset': 10, 'tracks': [{'n': 1, 'title': 'Song', 'dur': 20}]}
        albums = [{'artist': 'Example Alias', 'album': 'Live', 'dir': 'Example Artist - Live',
                   'tracks': [{'n': 11, 'file': 'Artist - Live/11.flac', 'size': 10}]}]
        with patch.object(webapp, 'get_toc_payload', return_value=toc), patch.object(
                webapp, 'list_library', side_effect=lambda: copy.deepcopy(albums)):
            shelf = webapp.get_shelf()
        self.assertEqual(shelf['albums'], [])
        self.assertEqual(shelf['cd']['tracks'][0]['local_file'], 'Artist - Live/11.flac')

    def test_queue_prefers_local_copy_and_keeps_order(self):
        shelf = {'cd': {'tracks': [{'n': 1, 'local_file': 'a/1.flac'}, {'n': 2}]},
                 'albums': [{'tracks': [{'file': 'b/1.flac', 'title': 'B'}]}]}
        with patch.object(webapp, 'get_shelf', return_value=shelf):
            self.assertEqual([e['uri'] for e in webapp.shelf_entries()],
                             ['a/1.flac', 'cdda:///dev/sr0/2', 'b/1.flac'])

    def test_unknown_queue_uri_does_not_clear_player(self):
        with patch.object(webapp, 'shelf_entries', return_value=[]), patch.object(webapp, '_mpd_run') as run:
            self.assertEqual(webapp.queue_and_play('missing')[0], 1)
            run.assert_not_called()

    def test_airplay_pause_freezes_elapsed(self):
        with patch.dict(webapp._ap, {'active': True, 'paused': False, 'elapsed': 10,
                                    'duration': 100, 'elapsed_at': 90}, clear=True), patch.object(
                webapp.time, 'time', return_value=100):
            webapp._ap_dispatch('ssnc', 'pfls', b'')
            self.assertTrue(webapp._ap['paused'])
            self.assertEqual(webapp._ap['elapsed'], 20)

    def test_airplay_latency_compensation(self):
        with patch.dict(webapp._ap, {}, clear=True), patch.object(webapp, '_AP_LAT', 0.44):
            webapp._ap_dispatch('ssnc', 'prgr', b'0/441000/4410000')
            self.assertAlmostEqual(webapp._ap['elapsed'], 9.56)
            self.assertEqual(webapp._ap['duration'], 100)

    def test_pcm_format_uses_effective_depth(self):
        self.assertEqual(webapp._fmt_kbps('s24-32le 2ch 48000Hz'),
                         {'rate': 48000, 'depth': 24, 'ch': 2, 'kbps': 2304})

    def test_metadata_unicode(self):
        self.assertEqual(cdmeta._parse_xmcd('DTITLE=Artist / Album\nDYEAR=2020\nTTITLE0=Song'),
                         {'artist': 'Artist', 'album': 'Album', 'year': '2020', 'tracks': ['Song']})

    def test_sanitize_preserves_useful_names(self):
        self.assertEqual(webapp._sanitize('  A/B\\C.  '), 'A_B_C')


if __name__ == '__main__':
    unittest.main()

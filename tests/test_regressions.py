import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airplay_hub import cdmeta
from airplay_hub import webapp

TOC = {'err': None, 'discid': '12345678', 'artist': 'Artist', 'album': 'Album',
       'year': None, 'tracks': [{'n': 1, 'title': 'Song'}]}


class ConfirmedDefects(unittest.TestCase):
    def setUp(self):
        from airplay_hub.optical import OpticalDrive
        for replacement in (patch.object(webapp, '_drive', OpticalDrive()),
                            patch.object(webapp, 'mpc', return_value=(0, ''))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_rip_reserves_slot_before_thread_runs(self):
        with patch.dict(webapp._rip, {'running': False}), patch.object(
                webapp, 'get_toc_payload', return_value=TOC), patch.object(webapp.threading, 'Thread'):
            self.assertTrue(webapp.start_rip(None)[0])
            self.assertFalse(webapp.start_rip(None)[0])

    def test_rip_invalid_track_never_starts(self):
        with patch.dict(webapp._rip, {'running': False}), patch.object(
                webapp, 'get_toc_payload', return_value=TOC), patch.object(webapp.threading, 'Thread') as thread:
            self.assertFalse(webapp.start_rip([99])[0])
            thread.assert_not_called()

    def test_failed_track_not_counted_as_complete(self):
        with patch.dict(webapp._rip, {}, clear=True), patch.object(
                webapp, '_rip_one_track', return_value=(False, 'read failed')), patch.object(
                webapp, 'mpc', return_value=(0, '')):
            webapp.rip_worker([(1, 'Song')], TOC, '12345678')
            self.assertEqual(webapp._rip['done'], 0)
            self.assertEqual(webapp._rip['finished_t'], 0)

    def test_worker_exception_is_visible(self):
        with patch.dict(webapp._rip, {}, clear=True), patch.object(
                webapp, '_rip_one_track', side_effect=OSError('disk full')), patch.object(
                webapp, 'mpc', return_value=(0, '')), self.assertLogs('airplay_hub.webapp'):
            webapp.rip_worker([(1, 'Song')], TOC, '12345678')
            self.assertIn('disk full', webapp._rip['err'])
            self.assertFalse(webapp._rip['running'])

    def test_local_path_rejected_before_queue_clear(self):
        with patch.object(webapp, 'mpc', return_value=(0, '')) as mpc:
            self.assertNotEqual(webapp.play_local('../secret.flac')[0], 0)
            mpc.assert_not_called()

    def test_mode_failure_is_reported(self):
        with patch.object(webapp, 'mpc', return_value=(1, 'MPD offline')):
            self.assertEqual(webapp.do_action({'action': 'mode', 'key': 'repeat', 'on': True})[0], 1)

    def test_cddb_exact_match(self):
        with patch.object(cdmeta, '_gate', side_effect=[
                '200 rock 12345678 Artist / Album\n',
                '210 OK\nDTITLE=Artist / Album\nTTITLE0=Song\n.\n']):
            meta = cdmeta.freedb_lookup('12345678', ['1', '150', '60'])
            self.assertIsNotNone(meta)
            self.assertEqual(meta['tracks'], ['Song'])

    def test_bluetooth_scan_admission_is_atomic(self):
        with patch.dict(webapp._bt_state, {'discovering': False}), patch.object(webapp.threading, 'Thread'):
            self.assertEqual(webapp.bt_scan()[0], 0)
            self.assertEqual(webapp.bt_scan()[0], 1)


if __name__ == '__main__':
    unittest.main()

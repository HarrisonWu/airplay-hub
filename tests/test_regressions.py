import json
import re
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

    # The wired sink name used to be the constant "output_wired", which only
    # existed on the machine it was written for. PulseAudio and PipeWire name
    # sinks after the hardware, so it has to be resolved at runtime.

    def test_wired_sink_skips_bluetooth_sinks(self):
        sinks = json.dumps([
            {'name': 'bluez_output.AA_BB_CC_DD_EE_FF.1', 'description': 'Speaker'},
            {'name': 'alsa_output.usb-DAC-00.analog-stereo', 'description': 'DAC'},
        ])
        with patch.object(webapp, 'WIRED_SINK', None), patch.object(
                webapp, 'run', return_value=(0, sinks)):
            self.assertEqual(webapp.wired_sink(),
                             'alsa_output.usb-DAC-00.analog-stereo')

    def test_wired_sink_override_avoids_pactl(self):
        with patch.object(webapp, 'WIRED_SINK', 'output_wired'), patch.object(
                webapp, 'run') as run:
            self.assertEqual(webapp.wired_sink(), 'output_wired')
            run.assert_not_called()

    def test_switch_output_refuses_a_missing_wired_sink(self):
        with patch.object(webapp, 'probe_audio_link',
                          return_value={'mode': 'wired', 'bt_sinks': []}), patch.object(
                webapp, 'wired_sink', return_value=None), patch.object(
                webapp, 'run') as run:
            rc, message = webapp.do_action({'action': 'switch_output', 'to': 'wired'})
            self.assertEqual(rc, 1)
            self.assertIn('wired', message)
            run.assert_not_called()

    def test_pipewire_bluetooth_codec_property_is_read(self):
        def fake_run(args, timeout=0):
            if 'info' in args:
                return 0, json.dumps({'default_sink_name': 'bluez_output.AA_BB.1'})
            if 'sinks' in args:
                return 0, json.dumps([{'name': 'bluez_output.AA_BB.1',
                                       'description': 'Speaker',
                                       'sample_specification': 's16le 2ch 44100Hz',
                                       'state': 'RUNNING'}])
            return 0, json.dumps([{'name': 'bluez_card.AA_BB',
                                   'active_profile': 'a2dp-sink',
                                   'properties': {'api.bluez5.codec': 'ldac'}}])

        with patch.object(webapp, 'run', side_effect=fake_run), patch.dict(
                webapp._link_cache, {'t': 0.0, 'v': None}, clear=True):
            link = webapp.probe_audio_link(max_age=0)
        self.assertEqual(link['mode'], 'bt')
        self.assertEqual(link['codec'], 'LDAC')
        self.assertEqual(link['kbps'], 990)

    # A per-user audio server keeps its socket behind XDG_RUNTIME_DIR. The unit
    # used to pair "User=..." with "/run/user/%U", but systemd does not expand %U
    # to the User= value in a system unit -- it resolves against the service
    # manager's own user, so the value arrived as /run/user/0 and every pactl
    # call failed. The same investigation found ProtectHome=yes blanking
    # /run/user, which hides the socket this unit exists to talk to.

    def _unit(self):
        unit = Path(__file__).resolve().parents[1] / 'deploy' / 'airplay-hub.service'
        return unit.read_text(encoding='utf-8')

    def test_unit_points_at_a_numeric_runtime_dir(self):
        found = re.search(r'^Environment=XDG_RUNTIME_DIR=(\S+)$', self._unit(), re.M)
        self.assertIsNotNone(found, 'the unit must set XDG_RUNTIME_DIR')
        self.assertRegex(found.group(1), r'^/run/user/[0-9]+$')

    def test_unit_does_not_blank_the_runtime_dir(self):
        found = re.search(r'^ProtectHome=(.*)$', self._unit(), re.M)
        self.assertIsNotNone(found, 'the unit must state ProtectHome explicitly')
        self.assertNotIn(found.group(1).strip(), ('yes', 'true', '1', 'tmpfs'))

    def test_deploy_installs_as_the_account_the_unit_runs_as(self):
        # deploy.py owns the bundle and re-imports it as that account; if the two
        # drift, the bundle lands with the wrong ownership and the import check
        # runs as an account that cannot reach the audio server.
        from scripts import deploy
        unit = self._unit()
        self.assertEqual(re.search(r'^User=(\S+)$', unit, re.M).group(1), deploy.ACCOUNT)
        self.assertEqual(re.search(r'^Group=(\S+)$', unit, re.M).group(1), deploy.GROUP)

    def test_unit_runtime_dir_matches_the_unit_account(self):
        # XDG_RUNTIME_DIR and User= are one setting in two halves.
        unit = self._unit()
        account = re.search(r'^User=(\S+)$', unit, re.M).group(1)
        if not account.isdigit():
            self.skipTest('User= is a name, so the UID cannot be compared directly')
        self.assertEqual(re.search(r'^Environment=XDG_RUNTIME_DIR=(\S+)$', unit, re.M).group(1),
                         '/run/user/' + account)

    def test_unreachable_audio_server_is_reported(self):
        with patch.object(webapp, 'run',
                          return_value=(1, 'Connection failure: Connection refused')):
            with self.assertLogs('airplay_hub.webapp', level='ERROR') as logs:
                self.assertFalse(webapp.check_audio_server())
        self.assertIn('XDG_RUNTIME_DIR', '\n'.join(logs.output))

    def test_reachable_audio_server_is_silent(self):
        with patch.object(webapp, 'run', return_value=(0, 'Server Name: PulseAudio')), \
                patch.object(webapp.logger, 'error') as error:
            self.assertTrue(webapp.check_audio_server())
        error.assert_not_called()


if __name__ == '__main__':
    unittest.main()

import copy
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from airplay_hub.background import RefreshWorker
from airplay_hub.optical import OpticalDrive
from airplay_hub.processes import run_until_quiet, terminate_and_wait
from airplay_hub import webapp
from test_regressions import TOC


@unittest.skipUnless(os.name == 'posix', 'Linux pipe selector integration')
class ProcessIntegration(unittest.TestCase):
    def child(self, code, **kwargs):
        return run_until_quiet([sys.executable, '-u', '-c', code], **kwargs)

    def test_partial_line_cannot_block(self):
        start = time.monotonic()
        rc, output = self.child("import time; print('partial',end='',flush=True); time.sleep(10)", quiet=.05, timeout=.3)
        self.assertEqual((rc, output), (0, 'partial'))
        self.assertLess(time.monotonic() - start, 2)

    def test_no_output_times_out(self):
        self.assertEqual(self.child('import time; time.sleep(10)', timeout=.05)[0], -1)

    def test_closed_stdout_does_not_await_exit_forever(self):
        rc, _ = self.child('import os,time; os.close(1); os.close(2); time.sleep(10)', timeout=.08)
        self.assertEqual(rc, -1)

    def test_nonzero_exit_is_preserved(self):
        self.assertEqual(self.child("print('failure'); raise SystemExit(7)")[0], 7)

    def test_output_cap(self):
        rc, output = self.child("print('x'*100000)", limit=1024)
        self.assertEqual(rc, -2)
        self.assertEqual(len(output), 1024)

    def test_ignored_terminate_escalates_and_reaps(self):
        proc = subprocess.Popen([sys.executable, '-u', '-c',
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(10)"],
            stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline(), b'ready\n')
            terminate_and_wait(proc, grace=.05)
            self.assertIsNotNone(proc.returncode)
            with self.assertRaises(ChildProcessError):
                os.waitpid(proc.pid, os.WNOHANG)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            proc.stdout.close()


class RefreshConcurrency(unittest.TestCase):
    def test_requests_share_one_worker_and_full_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def light():
            calls.append('light'); entered.set(); release.wait(2)
        worker = RefreshWorker(light, lambda: calls.append('full'))
        worker.request()
        self.assertTrue(entered.wait(1))
        try:
            threads=[threading.Thread(target=worker.request) for _ in range(20)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(1)
            worker.request(full=True)
            worker.request(full=True)
            self.assertEqual(calls, ['light'])
        finally:
            release.set()
        self.assertTrue(worker.idle.wait(2))
        self.assertEqual(calls, ['light', 'full'])

    def test_start_failure_allows_retry(self):
        calls=[]
        worker=RefreshWorker(lambda:calls.append(1), lambda:None)
        with patch('airplay_hub.background.threading.Thread', side_effect=RuntimeError('no threads')):
            with self.assertRaises(RuntimeError): worker.request()
        self.assertTrue(worker.idle.is_set())
        worker.request()
        self.assertTrue(worker.idle.wait(1))
        self.assertEqual(calls,[1])

    def test_callback_error_allows_retry(self):
        function=Mock(side_effect=[ValueError('bad probe'), None])
        worker=RefreshWorker(function, function)
        with self.assertLogs('airplay_hub.background'):
            worker.request(); self.assertTrue(worker.idle.wait(1))
        worker.request(); self.assertTrue(worker.idle.wait(1))
        self.assertEqual(function.call_count,2)

    def test_bluetooth_error_text_is_not_success(self):
        with patch.object(webapp,'run_until_quiet',return_value=(0,'Device not available')):
            self.assertEqual(webapp._btc_fast(['info','AA'])[0],1)

    def test_failed_controller_probe_preserves_last_snapshot(self):
        for refresh in (webapp._bt_light, webapp._bt_full):
            with patch.dict(webapp._bt_state, {'devices': [{'mac': 'AA'}], 'powered': True}, clear=True), patch.object(
                    webapp, '_btc_fast', return_value=(-1, 'controller timeout')):
                refresh()
                self.assertEqual(webapp._bt_state['devices'], [{'mac': 'AA'}])
                self.assertIn('timeout', webapp._bt_state['err'])


class OpticalConcurrency(unittest.TestCase):
    def setUp(self):
        for replacement in (patch.object(webapp,'_drive',OpticalDrive()),
                            patch.dict(webapp._toc,{'payload':None,'t':0},clear=True),
                            patch.dict(webapp._rip,{'running':False},clear=True),
                            patch.object(webapp,'mpc',return_value=(0,''))):
            replacement.start(); self.addCleanup(replacement.stop)

    def test_only_one_expensive_toc_probe(self):
        entered, release=threading.Event(), threading.Event()
        def read():
            entered.set(); release.wait(2); return copy.deepcopy(TOC)
        with patch.object(webapp,'_read_toc_payload',side_effect=read) as probe:
            thread=threading.Thread(target=webapp.get_toc_payload)
            thread.start(); self.assertTrue(entered.wait(1))
            try:
                self.assertIn('busy',webapp.get_toc_payload()['err'])
                self.assertEqual(probe.call_count,1)
            finally:
                release.set(); thread.join(2)
        self.assertIsNone(webapp._drive.operation)

    def test_busy_read_returns_isolated_stale_snapshot(self):
        webapp._toc.update(payload=copy.deepcopy(TOC),t=0)
        token=webapp._drive.claim('rip')
        try:
            result=webapp.get_toc_payload(); result['tracks'].clear()
            self.assertEqual(len(webapp._toc['payload']['tracks']),1)
        finally: webapp._drive.release(token)

    def test_reader_exception_releases_drive(self):
        with patch.object(webapp,'_read_toc_payload',side_effect=OSError('unplugged')):
            with self.assertRaises(OSError): webapp.get_toc_payload()
        self.assertIsNone(webapp._drive.operation)

    def test_cd_playback_uses_cache_without_probing(self):
        with patch.object(webapp,'_cd_playback_active',return_value=True), patch.object(webapp,'_read_toc_payload') as read:
            self.assertIn('playback',webapp.get_toc_payload()['err'])
            read.assert_not_called()

    def test_mixed_queue_protects_automatic_cd_transition(self):
        with patch.object(webapp, 'mpc', side_effect=[(0, 'local.flac\ncdda:///dev/sr0/1'), (0, '[playing]')]):
            self.assertTrue(webapp._cd_playback_active())

    def test_unknown_player_state_does_not_allow_hardware_probe(self):
        with patch.object(webapp, 'mpc', return_value=(1, 'MPD unavailable')):
            self.assertTrue(webapp._cd_playback_active())

    def test_reidentify_rejected_while_drive_in_use(self):
        token=webapp._drive.claim('rip')
        try:
            self.assertEqual(webapp.do_action({'action':'reidentify'})[0],1)
            webapp.mpc.assert_not_called()
        finally: webapp._drive.release(token)

    def test_transport_cannot_restart_cd_during_rip_or_toc(self):
        for operation in ('rip','toc'):
            token=webapp._drive.claim(operation)
            try:
                for action in ('play','toggle','next','prev','play_cd_disc'):
                    self.assertEqual(webapp.do_action({'action':action})[0],1)
                self.assertEqual(webapp.do_action({'action':'seek', 'pos':2})[0],1)
                webapp.mpc.assert_not_called()
            finally: webapp._drive.release(token)

    def test_playback_can_read_toc_before_starting(self):
        with patch.object(webapp,'_read_toc_payload',return_value=TOC) as read, patch.object(
                webapp,'_mpd_run',return_value=(0,'')):
            self.assertEqual(webapp.do_action({'action':'play_cd_disc'})[0],0)
            read.assert_called_once()
        self.assertIsNone(webapp._drive.operation)

    def test_stop_failure_does_not_start_rip_or_leak_drive(self):
        with patch.object(webapp,'get_toc_payload',return_value=TOC), patch.object(
                webapp,'mpc',return_value=(1,'offline')), patch.object(webapp.threading,'Thread') as thread:
            self.assertEqual(webapp.start_rip(None),(False,'offline'))
            thread.assert_not_called()
        self.assertIsNone(webapp._drive.operation)
        self.assertFalse(webapp._rip['running'])

    def test_worker_failure_releases_transferred_ownership(self):
        token=webapp._drive.claim('rip')
        with patch.object(webapp,'_rip_one_track',side_effect=OSError('disk full')), self.assertLogs('airplay_hub.webapp'):
            thread=threading.Thread(target=webapp.rip_worker,args=([(1,'Song')],TOC,'12345678',token))
            thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(webapp._drive.operation)
        self.assertEqual(webapp._rip['err'],'disk full')


if __name__=='__main__': unittest.main()

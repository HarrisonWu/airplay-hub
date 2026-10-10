from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy


class DeploymentBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'target'
        self.target.mkdir()
        (self.target / 'airplay_hub').mkdir()
        (self.target / 'airplay_hub/webapp.py').write_bytes(b'old application')
        self.baseline = {name: None for name in deploy.FILES}
        self.baseline['airplay_hub/webapp.py'] = deploy.digest(
            self.target / 'airplay_hub/webapp.py')

    def test_drift_including_new_files_is_rejected(self):
        deploy.check_baseline(self.target, self.baseline)
        (self.target / 'airplay_hub/optical.py').write_bytes(b'external work')
        with self.assertRaisesRegex(ValueError, 'optical.py'):
            deploy.check_baseline(self.target, self.baseline)

    def test_incomplete_baseline_is_rejected(self):
        with self.assertRaises(ValueError): deploy.check_baseline(self.target, {})

    def test_partial_install_can_restore_latest_external_source(self):
        backup = self.root / 'backup'
        deploy.save_backup(self.target, backup, self.baseline)
        (self.target / 'airplay_hub/webapp.py').write_bytes(b'new application')
        (self.target / 'airplay_hub/optical.py').write_bytes(b'new helper')
        with patch.object(deploy.os, 'chown', create=True):
            deploy.restore(self.target, backup)
        self.assertEqual((self.target / 'airplay_hub/webapp.py').read_bytes(), b'old application')
        self.assertEqual((self.target / 'airplay_hub/optical.py').read_bytes(), b'new helper')

    def test_failed_publish_keeps_original_and_cleans_temp(self):
        with patch.object(deploy.os, 'chown', create=True), patch.object(
                deploy.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                deploy.write_atomic(self.target / 'airplay_hub/webapp.py', b'new', {'uid': 0, 'gid': 0, 'mode': 0o644})
        self.assertEqual((self.target / 'airplay_hub/webapp.py').read_bytes(), b'old application')
        self.assertEqual(list(self.target.glob('.deploy-*')), [])

    def test_corrupt_backup_is_rejected_before_overwriting(self):
        backup = self.root / 'backup'
        deploy.save_backup(self.target, backup, self.baseline)
        (backup / 'airplay_hub/webapp.py').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Backup damaged'):
            deploy.restore(self.target, backup)
        self.assertEqual((self.target / 'airplay_hub/webapp.py').read_bytes(), b'old application')


if __name__ == '__main__': unittest.main()

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import promote_backup as p


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / 'output'
        self.dest = self.base / 'coprint'
        self.dest.mkdir()
        self.old = self.dest / 'coprint-stock.img'
        self.old.write_bytes(b'original stock recovery image')
        (self.dest / 'coprint.img').symlink_to(self.old.name)
        self.release_dir = self.output / 'debian-recovery-20261025T200000Z'
        self.release_dir.mkdir(parents=True)
        self.image = self.release_dir / 'chromapad-debian-recovery-auto.img'
        self.image.write_bytes(b'new Debian automatic recovery image')
        manifest = self.release_dir / 'manifest.json'
        manifest.write_text('{"fixture":true}')
        self.release = {'status': 'VERIFIED_AUTOMATIC_RECOVERY_IMAGE', 'automatic_restore': True,
                        'build_id': self.release_dir.name, 'image': str(self.image),
                        'bytes': self.image.stat().st_size, 'sha256': p.digest(self.image),
                        'manifest_sha256': p.digest(manifest)}
        self.write_release()
        (self.release_dir / 'validation').mkdir()
        for name in ('base-built', 'config-built', 'initramfs-report', 'image-validation',
                     'launcher-smoke', 'usb-layout', 'inputs'):
            (self.release_dir / 'validation' / (name + '.json')).write_text(json.dumps({'status': 'PASS'}))
        self.before = p.anchor_identity(self.dest)

    def write_release(self):
        (self.release_dir / 'release.json').write_text(json.dumps(self.release))

    def publish(self):
        return p.publish(self.image, self.output, self.dest, self.before)

    def assert_old(self):
        self.assertEqual((self.dest / 'coprint.img').read_bytes(), b'original stock recovery image')

    def test_success_is_exact_hardlink_and_preserves_stock(self):
        result = self.publish()
        self.assertEqual(result['status'], 'COPRINT_IMAGE_PUBLISHED')
        self.assertTrue(os.path.samefile(self.dest / 'coprint.img', self.image))
        self.assertEqual(self.old.read_bytes(), b'original stock recovery image')
        self.assertEqual(p.digest(self.image), self.release['sha256'])

    def test_checksum_failure_leaves_anchor_unchanged(self):
        self.image.write_bytes(b'corrupted same or different length')
        with self.assertRaises(RuntimeError): self.publish()
        self.assert_old()

    def test_failed_gate_leaves_anchor_unchanged(self):
        (self.release_dir / 'validation/image-validation.json').write_text('{"status":"FAIL"}')
        with self.assertRaisesRegex(RuntimeError, 'Failed release gate'): self.publish()
        self.assert_old()

    def test_gate_pin_mismatch_refused(self):
        (self.release_dir / 'validation/image-validation.json').write_text('{"status":"PASS","manifest_sha256":"wrong"}')
        with self.assertRaisesRegex(RuntimeError, 'pin mismatch'): self.publish()
        self.assert_old()

    def test_manifest_tamper_refused(self):
        (self.release_dir / 'manifest.json').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'Manifest mismatch'): self.publish()
        self.assert_old()

    def test_changed_anchor_refused(self):
        self.old.write_bytes(b'other job changed this image')
        with self.assertRaisesRegex(RuntimeError, 'changed during backup'): self.publish()
        self.assertEqual((self.dest / 'coprint.img').read_bytes(), b'other job changed this image')

    def test_anchor_path_escape_refused(self):
        (self.dest / 'coprint.img').unlink()
        (self.dest / 'coprint.img').symlink_to(self.image)
        with self.assertRaisesRegex(RuntimeError, 'escapes'): p.anchor_identity(self.dest)

    def test_regular_old_anchor_is_preserved_before_switch(self):
        (self.dest / 'coprint.img').unlink()
        (self.dest / 'coprint.img').write_bytes(b'old regular image')
        self.before = p.anchor_identity(self.dest)
        result = self.publish()
        self.assertEqual(Path(result['previous_image_preserved']).read_bytes(), b'old regular image')
        self.assertTrue(os.path.samefile(self.dest / 'coprint.img', self.image))

    def test_atomic_switch_failure_retains_old_image(self):
        replace = os.replace
        def fail_switch(src, dst):
            if Path(dst).name == 'coprint.img': raise OSError('injected interruption')
            return replace(src, dst)
        with patch.object(p.os, 'replace', side_effect=fail_switch):
            with self.assertRaisesRegex(OSError, 'interruption'): self.publish()
        self.assert_old()
        self.assertFalse(list(self.dest.glob('.coprint-pending-*')))

    def test_hardlink_failure_retains_old_image(self):
        with patch.object(p.os, 'link', side_effect=OSError('unsupported')):
            with self.assertRaises(OSError): self.publish()
        self.assert_old()

    def test_existing_dated_image_not_overwritten(self):
        dated = self.dest / ('coprint_' + self.release_dir.name + '.img')
        dated.write_bytes(b'existing generation')
        with self.assertRaisesRegex(RuntimeError, 'already exists'): self.publish()
        self.assertEqual(dated.read_bytes(), b'existing generation')
        self.assert_old()

    def test_release_from_other_directory_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'outside release'): p.verify_release(self.image, self.base)

    def test_completed_builder_output_parsed(self):
        text = 'progress {not json}\n' + json.dumps(self.release, indent=2)
        self.assertEqual(p.completed_release(text), self.release)

    def test_missing_or_ambiguous_result_refused(self):
        for text in ('build failed', json.dumps(self.release) + json.dumps(self.release)):
            with self.assertRaises(RuntimeError): p.completed_release(text)


if __name__ == '__main__':
    unittest.main(verbosity=2)

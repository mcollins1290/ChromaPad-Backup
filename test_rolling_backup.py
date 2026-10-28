import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import rolling_backup as p
import recreate_recovery_image as recreate
import types


class RollingOwnershipTests(unittest.TestCase):
    def test_nas_automount_wrapper_is_accepted(self):
        p.verify_nas_mount([{'fstype': 'autofs', 'source': 'systemd-1'},
                            {'fstype': 'nfs4', 'source': '192.168.0.2:/media/raid1'}])

    def test_unmounted_or_wrong_nas_is_refused(self):
        for mounts in ([{'fstype': 'autofs', 'source': 'systemd-1'}],
                       [{'fstype': 'ext4', 'source': '/dev/mmcblk1p2'}],
                       [{'fstype': 'nfs4', 'source': 'other:/data'}]):
            with self.subTest(mounts=mounts), self.assertRaises(RuntimeError):
                p.verify_nas_mount(mounts)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / 'coprint-debian.img'
        self.state = self.root / 'coprint-debian.json'
        self.size = 1024 * 1024

    def owned(self):
        with self.image.open('xb') as f:
            f.truncate(self.size)
        record = {'kind': 'chromapad-rolling-image', 'image': str(self.image),
                  'inode': self.image.stat().st_ino, 'bytes': self.size, 'state': 'FAILED'}
        self.state.write_text(json.dumps(record))
        return record

    def test_missing_image_is_first_run(self):
        self.assertIsNone(p.managed_record(self.image, self.state, self.size))

    def test_existing_unowned_image_is_not_overwritten(self):
        self.image.write_bytes(bytes(self.size))
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            p.managed_record(self.image, self.state, self.size)

    def test_interrupted_owned_image_is_reusable(self):
        rec = self.owned()
        self.assertEqual(p.managed_record(self.image, self.state, self.size), rec)

    def test_replaced_inode_is_refused(self):
        self.owned()
        replacement = self.root / 'different'
        replacement.write_bytes(bytes(self.size))
        os.replace(replacement, self.image)
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            p.managed_record(self.image, self.state, self.size)

    def test_hardlinked_target_is_refused(self):
        self.owned()
        os.link(self.image, self.root / 'other-name')
        with self.assertRaises(RuntimeError):
            p.managed_record(self.image, self.state, self.size)

    def test_symlink_target_is_refused(self):
        self.owned()
        target = self.root / 'real'
        self.image.rename(target)
        self.image.symlink_to(target)
        with self.assertRaises(RuntimeError):
            p.managed_record(self.image, self.state, self.size)

    def test_missing_owned_image_is_not_silently_recreated(self):
        self.owned()
        self.image.unlink()
        with self.assertRaises(RuntimeError):
            p.managed_record(self.image, self.state, self.size)

    def test_space_is_one_image_growth_not_45_gib(self):
        self.assertEqual(p.required_space(self.image, self.size), self.size + 512 * 1024**2)
        self.owned()
        self.assertLessEqual(p.required_space(self.image, self.size), self.size + 512 * 1024**2)

    def test_first_activation_preserves_stock_then_updates_same_inode(self):
        self.owned()
        stock = self.root / 'stock.img'
        stock.write_bytes(b'preserve stock')
        anchor = self.root / 'coprint.img'
        anchor.symlink_to(stock.name)
        before = p.anchor_identity(self.root)
        inode = self.image.stat().st_ino
        with patch.object(p, 'DEST', self.root):
            p.activate(before, self.image)
            before = p.anchor_identity(self.root)
            p.activate(before, self.image)
        self.assertEqual(stock.read_bytes(), b'preserve stock')
        self.assertEqual(self.image.stat().st_ino, inode)
        self.assertEqual(anchor.resolve(), self.image)
        self.assertEqual(sorted(x.name for x in self.root.glob('*.img')),
                         ['coprint-debian.img', 'coprint.img', 'stock.img'])

    def test_competing_anchor_change_is_refused(self):
        self.owned()
        (self.root / 'stock.img').write_bytes(b'stock')
        anchor = self.root / 'coprint.img'
        anchor.symlink_to('stock.img')
        before = p.anchor_identity(self.root)
        anchor.unlink()
        anchor.symlink_to(self.image.name)
        with patch.object(p, 'DEST', self.root), self.assertRaisesRegex(RuntimeError, 'changed'):
            p.activate(before, self.image)

    def test_capacity_policy_grows_only_when_free_space_is_insufficient(self):
        gib = 1024**3
        self.assertEqual(p.capacity_for_growth(12*gib, 3*gib, 0), 0)
        self.assertEqual(p.capacity_for_growth(12*gib, 3*gib, -gib), 0)
        self.assertEqual(p.capacity_for_growth(12*gib, 3*gib, gib), gib//2)

    def test_failed_or_interrupted_backups_require_content_checks(self):
        self.assertTrue(p.needs_content_check(None))
        self.assertTrue(p.needs_content_check({'state':'FAILED'}))
        self.assertTrue(p.needs_content_check({'state':'UPDATING'}))
        self.assertTrue(p.needs_content_check({'state':'READY','needs_source_checksum':True}))
        self.assertFalse(p.needs_content_check({'state':'READY'}))

    def test_storage_errors_fail_even_when_copy_commands_succeeded(self):
        (self.root/'reports').mkdir()
        fake = types.SimpleNamespace(b=types.SimpleNamespace(run=lambda *args: json.dumps({'MESSAGE':
            'EXT4-fs (loop2): failed to convert unwritten extents to written extents -- potential data loss!'}),
            record=lambda path,data:path.write_text(json.dumps(data))))
        with self.assertRaisesRegex(RuntimeError, 'Kernel reported storage errors'):
            p.check_kernel_storage(fake, self.root, 'cursor')
        report=json.loads((self.root/'reports/kernel-storage.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        fake.b.run=lambda *args: json.dumps({'MESSAGE':'hid-multitouch: device initialized'})
        p.check_kernel_storage(fake, self.root, 'cursor')

    def test_recreation_publishes_only_the_verified_candidate_and_preserves_snapshot(self):
        record = self.owned()
        record.update(state='READY', result={'bytes': self.size, 'root_sha256': 'saved'})
        self.state.write_text(json.dumps(record))
        candidate = self.root/'.coprint-debian.rebuild.img'
        candidate.write_bytes(b'new container')
        result = {'candidate_inode': candidate.stat().st_ino, 'image_bytes': candidate.stat().st_size,
                  'candidate_mtime_ns': candidate.stat().st_mtime_ns}
        with patch.object(p, 'IMAGE', self.image), patch.object(p, 'STATE', self.state):
            updated = recreate.publish(candidate, record, result, self.image.stat())
        self.assertEqual(self.image.read_bytes(), b'new container')
        self.assertEqual(updated['result']['root_sha256'], 'saved')
        self.assertEqual(updated['state'], 'READY')
        self.assertFalse(candidate.exists())
        self.assertFalse((self.root/'.coprint-debian.previous.img').exists())

    def test_recreation_failed_publication_restores_the_old_image(self):
        record = self.owned()
        record.update(state='READY', result={'bytes': self.size})
        self.state.write_text(json.dumps(record))
        candidate = self.root/'.coprint-debian.rebuild.img'
        candidate.write_bytes(b'new container')
        result = {'candidate_inode': candidate.stat().st_ino, 'image_bytes': candidate.stat().st_size,
                  'candidate_mtime_ns': candidate.stat().st_mtime_ns}
        original = p.save_state
        calls = 0
        def fail_once(rec, owner):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('publication failed')
            original(rec, owner)
        with patch.object(p, 'IMAGE', self.image), patch.object(p, 'STATE', self.state), \
                patch.object(p, 'save_state', side_effect=fail_once):
            with self.assertRaises(OSError):
                recreate.publish(candidate, record, result, self.image.stat())
        self.assertEqual(self.image.stat().st_ino, record['inode'])
        self.assertEqual(json.loads(self.state.read_text()), record)
        self.assertTrue(candidate.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)

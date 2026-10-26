import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib
import chromapad_debian_backup as b


class BackupSafetyTests(unittest.TestCase):
    def test_cleanup_waits_for_copy_child_then_detaches(self):
        busy = subprocess.CalledProcessError(32, ['umount', '/tmp/root'])
        with patch.object(b, 'command', side_effect=[busy, None, None]) as command, patch.object(b.time, 'sleep') as sleep, patch.object(b, 'run', return_value='/tmp/image'):
            b.cleanup_image(Path('/tmp/image'), ['/dev/loop8'], [('/dev/loop8', Path('/tmp/root'))])
        self.assertEqual(command.call_count, 3)
        sleep.assert_called_once_with(1)
        self.assertEqual(command.call_args.args, ('losetup', '-d', '/dev/loop8'))

    def test_busy_partition_retained_other_partition_cleaned(self):
        def unmount(point):
            if point == Path('/tmp/root'):
                raise subprocess.CalledProcessError(32, ['umount', str(point)])
        with patch.object(b, 'unmount_image_point', side_effect=unmount), patch.object(b, 'command') as command, patch.object(b, 'run', return_value='/tmp/image'):
            with self.assertRaisesRegex(RuntimeError, 'Image cleanup incomplete'):
                b.cleanup_image(Path('/tmp/image'), ['/dev/loop8', '/dev/loop9'], [('/dev/loop8', Path('/tmp/fat')), ('/dev/loop9', Path('/tmp/root'))])
        command.assert_called_once_with('losetup', '-d', '/dev/loop8')

    def test_cleanup_refuses_reassigned_loop(self):
        with patch.object(b, 'command') as command, patch.object(b, 'run', return_value='/tmp/other-image'):
            with self.assertRaisesRegex(RuntimeError, 'backing file changed'):
                b.cleanup_image(Path('/tmp/image'), ['/dev/loop8'], [])
        command.assert_not_called()

    def test_unmount_retry_bounded_and_never_forced(self):
        with patch.object(b, 'command', side_effect=subprocess.CalledProcessError(32, ['umount'])) as command, patch.object(b.time, 'sleep'):
            with self.assertRaises(subprocess.CalledProcessError):
                b.unmount_image_point(Path('/tmp/root'), attempts=3)
        self.assertEqual(command.call_count, 3)
        for call in command.call_args_list:
            self.assertEqual(call.args, ('umount', Path('/tmp/root')))

    def test_excluded_runtime_directory_mtime_only(self):
        result = b.comparison_result('.d..t...... run/\n')
        self.assertEqual(result['blocking'], [])
        self.assertEqual(result['ignored_volatile_directory_mtimes'], ['.d..t...... run/'])

    def test_runtime_directory_permissions_not_ignored(self):
        self.assertTrue(b.comparison_result('.d...p..... run/\n')['blocking'])

    def test_regular_file_and_other_directory_changes_not_ignored(self):
        for line in ['.f..t...... run/file', '.d..t...... etc/', '>fcs....... etc/passwd']:
            self.assertTrue(b.comparison_result(line)['blocking'])

    def test_rejects_stock_destination(self):
        with self.assertRaises(RuntimeError):
            b.check_destination(b.STOCK)

    def test_rejects_arbitrary_destination(self):
        with self.assertRaises(RuntimeError):
            b.check_destination(Path('/tmp/backup'))

    def test_owner_change_rejects_outside_backup_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, 'Unsafe publication directory'):
                b.owner_access(Path(d))

    def test_wrong_mbr(self):
        with self.assertRaises(RuntimeError):
            b.validate_mbr(bytes(512))

    def test_short_region_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'file'; p.write_bytes(b'abc')
            with self.assertRaises(RuntimeError):
                b.digest(p, 0, 4)

    def test_no_mount_of_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'file'; p.touch()
            link = Path(d)/'link'; link.symlink_to(p)
            with self.assertRaises(RuntimeError), b.mounted_image(link, Path(d)):
                self.fail('Accepted symlink')

    def test_no_mount_of_wrong_size(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'file'; p.touch()
            with self.assertRaises(RuntimeError), b.mounted_image(p, Path(d)):
                self.fail('Accepted short file')

    def make_boot(self, work):
        fat, root = work/'fat', work/'root'
        gen = fat/'debian/test-generation'; gen.mkdir(parents=True)
        for folder in ['etc/udev/rules.d', 'root', 'boot', 'usr/lib/chromapad/emmc']:
            (root/folder).mkdir(parents=True, exist_ok=True)
        args = ['root=UUID='+b.ROOT_UUID, 'console=ttyAML0,115200n8', 'fsck.repair=preen']+b.USB_ARGS
        script = ("setenv bootargs '"+' '.join(args)+"'\nfatload mmc 1:1 0x100 debian/test-generation/Image\n").encode()
        payload = struct.pack('>II', len(script)+1, 0)+script+b'\0'
        header = bytearray(64)
        struct.pack_into('>7I', header, 0, 0x27051956, 0, 0, len(payload), 0, 0, zlib.crc32(payload))
        header[30] = 6
        struct.pack_into('>I', header, 4, zlib.crc32(header))
        (gen/'emmc_autoscript').write_bytes(header+payload)
        (fat/'emmc_autoscript').write_bytes(header+payload)
        (gen/'Image').write_bytes(b'TEST IMAGE')
        b.record(gen/'boot-manifest.json', {'files': {p.name: {'bytes': p.stat().st_size, 'sha256': b.digest(p)} for p in gen.iterdir()}})
        (root/'etc/fstab').write_text('UUID='+b.ROOT_UUID+' / ext4 defaults 0 1\nUUID='+b.FAT_UUID+' /boot/emmc vfat defaults 0 2\n')
        (root/'root/.bashrc').write_text('PS1=custom\n')
        (root/'etc/udev/rules.d/99-chromapad-usb-storage-no-media-poll.rules').touch()
        b.record(root/'usr/lib/chromapad/emmc/identifiers.json', {'root_uuid': b.ROOT_UUID, 'fat_uuid': b.FAT_UUID, 'kernel': 'test'})
        for prefix in ['vmlinuz-', 'initrd.img-']:
            (root/'boot'/(prefix+'test')).touch()
        return fat, root

    def test_valid_boot_and_corrupt_asset(self):
        with tempfile.TemporaryDirectory() as d:
            fat, root = self.make_boot(Path(d))
            self.assertEqual(b.validate_boot(fat, root)['status'], 'PASS')
            (fat/'debian/test-generation/Image').write_bytes(b'CORRUPTION')
            with self.assertRaisesRegex(RuntimeError, 'Boot asset changed'):
                b.validate_boot(fat, root)

    def test_bad_boot_crc(self):
        with tempfile.TemporaryDirectory() as d:
            fat, root = self.make_boot(Path(d))
            p = fat/'emmc_autoscript'; data = bytearray(p.read_bytes()); data[-4] ^= 1; p.write_bytes(data)
            with self.assertRaisesRegex(RuntimeError, 'CRC'):
                b.validate_boot(fat, root)

    def test_manifest_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            fat, root = self.make_boot(Path(d))
            b.record(fat/'debian/test-generation/boot-manifest.json', {'files': {'../../../outside': {}}})
            with self.assertRaisesRegex(RuntimeError, 'Unsafe boot manifest'):
                b.validate_boot(fat, root)

    def test_wrong_fstab(self):
        with tempfile.TemporaryDirectory() as d:
            fat, root = self.make_boot(Path(d))
            (root/'etc/fstab').write_text('UUID=stock / ext4 defaults 0 1\n')
            with self.assertRaisesRegex(RuntimeError, 'fstab'):
                b.validate_boot(fat, root)


if __name__ == '__main__':
    unittest.main(verbosity=2)

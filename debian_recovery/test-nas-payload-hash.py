import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch
import nas_payload_hash as nas
import rolling_recovery as rolling


class NasHashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stage = Path(self.tmp.name)
        (self.stage / 'reports').mkdir()
        self.image = self.stage / 'image.img'
        self.image.write_bytes(bytes(1024))
        self.m = {'usb_disk_id': '1234abcd', 'usb_root_uuid': str(uuid.uuid4())}
        self.report = {'schema': 1, 'location': 'NAS', 'cleanup_complete': True,
                       'bytes': rolling.r.RAW_BYTES, 'root_uuid': rolling.b.ROOT_UUID,
                       'usb_root_uuid': self.m['usb_root_uuid'], 'sha256': 'a' * 64}

    def test_pad_refuses_nas_hash_while_image_is_attached(self):
        with patch.object(rolling.b, 'run', return_value='/dev/loop0'), patch.object(rolling, 'nas_operation') as call:
            with self.assertRaisesRegex(RuntimeError, 'Unmount'):
                rolling.hash_payload_on_nas(self.image, self.stage, self.m)
            call.assert_not_called()

    def test_pad_validates_remote_identity_digest_and_cleanup(self):
        with patch.object(rolling.b, 'run', return_value=''), patch.object(rolling.c, 'USB_BYTES', 1024):
            for key, value in [('cleanup_complete', False), ('bytes', 1), ('root_uuid', 'wrong'),
                               ('usb_root_uuid', 'wrong'), ('sha256', 'invalid')]:
                report = {**self.report, key: value}
                with self.subTest(key=key), patch.object(rolling, 'nas_operation', return_value=report):
                    with self.assertRaisesRegex(RuntimeError, 'does not match'):
                        rolling.hash_payload_on_nas(self.image, self.stage, self.m)
            with patch.object(rolling, 'nas_operation', return_value=self.report):
                self.assertEqual(rolling.hash_payload_on_nas(self.image, self.stage, self.m), 'a' * 64)
            self.assertEqual(json.loads((self.stage / 'reports/payload-hash.json').read_text()), self.report)

    def test_ssh_failure_or_missing_result_is_rejected(self):
        for returncode, stdout in [(1, ''), (0, ''), (0, 'NAS_HASH_RESULT {}\n')]:
            result = subprocess.CompletedProcess([], returncode, stdout, 'test diagnostic')
            with self.subTest(returncode=returncode, stdout=stdout), patch.object(rolling.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError):
                    rolling.nas_operation({'operation': 'probe'}, self.stage)
        command = None
        def remote(args, **kwargs):
            nonlocal command
            command = args
            self.assertIn('REQUEST =', kwargs['input'])
            return subprocess.CompletedProcess([], 0, 'NAS_HASH_RESULT {"schema":1,"location":"NAS","ready":true}\n', '')
        with patch.object(rolling.subprocess, 'run', side_effect=remote):
            rolling.nas_preflight()
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('BatchMode=yes', command)

    def test_preflight_rejects_surviving_remote_loop(self):
        with patch.object(nas, 'LOCK', self.stage / 'lock'), \
             patch.object(nas.os, 'geteuid', return_value=0), \
             patch.object(nas.shutil, 'which', return_value='/usr/bin/tool'), \
             patch.object(nas.signal, 'signal'), patch.object(nas, 'run', return_value='/dev/loop0'):
            with self.assertRaisesRegex(RuntimeError, 'already has loop'):
                nas.main({'operation': 'probe'})

    def test_remote_rejects_changed_image_identity_and_geometry(self):
        header = bytearray(1024)
        header[510:512] = b'\x55\xaa'
        struct.pack_into('<I', header, 440, int(self.m['usb_disk_id'], 16))
        struct.pack_into('<II', header, 470, 1, 1)
        self.image.write_bytes(header)
        request = {'image_bytes': 1024, 'image_inode': self.image.stat().st_ino,
                   'usb_disk_id': self.m['usb_disk_id'], 'start_sector': 1, 'sectors': 1}
        with patch.object(nas, 'IMAGE', self.image):
            nas.image_identity(self.image, request)
            for key, value in [('image_bytes', 512), ('image_inode', 0), ('usb_disk_id', '00000000'),
                               ('start_sector', 2), ('sectors', 2)]:
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    nas.image_identity(self.image, {**request, key: value})

    def test_remote_hash_matches_sha256_and_rejects_unclean_payload(self):
        data = bytearray(8192)
        struct.pack_into('<I', data, 1028, 8)
        struct.pack_into('<HH', data, 1080, 0xef53, 1)
        root_uuid = str(uuid.uuid4())
        data[1128:1144] = uuid.UUID(root_uuid).bytes
        self.image.write_bytes(data)
        request = {'root_uuid': root_uuid, 'usb_root_uuid': self.m['usb_root_uuid'], 'raw_bytes': len(data)}
        self.assertEqual(nas.hash_file(self.image, request)['sha256'], hashlib.sha256(data).hexdigest())
        for field, value, error in [(1082, 0, 'not clean'), (1080, 0, 'not ext4')]:
            changed = bytearray(data)
            struct.pack_into('<H', changed, field, value)
            self.image.write_bytes(changed)
            with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                nas.hash_file(self.image, request)
        self.image.unlink()
        self.image.symlink_to(self.stage / 'other')
        with self.assertRaises(RuntimeError):
            nas.hash_file(self.image, request)

    def test_remote_failure_cleans_up_and_success_requires_cleanup(self):
        request = {'start_sector': 1050624, 'sectors': 50331648, 'usb_root_uuid': self.m['usb_root_uuid']}
        def run(*args):
            if args[:2] == ('losetup', '-j'):
                return ''
            if args[0] == 'blkid':
                return self.m['usb_root_uuid']
            if 'BACK-FILE' in args:
                return str(self.image)
            return '/dev/loop-fixture'
        mkdtemp = nas.tempfile.mkdtemp
        with patch.object(nas.tempfile, 'mkdtemp', side_effect=lambda **kw: mkdtemp(dir=self.stage, **kw)), \
             patch.object(nas, 'IMAGE', self.image), patch.object(nas, 'LOCK', self.stage / 'lock'), \
             patch.object(nas, 'image_identity', return_value=self.image.stat()), \
             patch.object(nas, 'run', side_effect=run), patch.object(nas.subprocess, 'run') as commands:
            with patch.object(nas, 'hash_file', side_effect=RuntimeError('read failed')):
                with self.assertRaisesRegex(RuntimeError, 'read failed'):
                    nas.hash_image(request)
            self.assertEqual([c.args[0][0] for c in commands.call_args_list], ['mount', 'umount', 'losetup'])
            commands.reset_mock()
            with patch.object(nas, 'hash_file', return_value={'sha256': 'a' * 64}):
                self.assertTrue(nas.hash_image(request)['cleanup_complete'])
            def fail_unmount(args, **kwargs):
                if args[0] == 'umount':
                    raise subprocess.CalledProcessError(1, args)
            commands.side_effect = fail_unmount
            with patch.object(nas, 'hash_file', return_value={'sha256': 'a' * 64}):
                with self.assertRaises(subprocess.CalledProcessError):
                    nas.hash_image(request)


if __name__ == '__main__':
    unittest.main(verbosity=2)

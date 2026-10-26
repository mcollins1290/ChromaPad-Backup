import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import uuid
from unittest.mock import patch
import chromapad_recovery as r
import rolling_recovery as rolling


class RawPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = bytearray(8192)
        struct.pack_into('<I', self.raw, 1028, 8)
        struct.pack_into('<HH', self.raw, 1080, 0xef53, 1)
        self.raw[1128:1144] = uuid.UUID(r.ROOT_UUID).bytes
        self.path = self.root / 'root.ext4'
        self.path.write_bytes(self.raw)
        checksum = hashlib.sha256(self.raw).hexdigest()
        self.m = {'schema': 2, 'root_payload': {'encoding': 'raw', 'file': 'root.ext4',
                  'bytes': 8192, 'raw_bytes': 8192, 'sha256': checksum, 'raw_sha256': checksum}}

    def test_raw_payload_restores_exact_bytes(self):
        dest = self.root / 'restored'
        with dest.open('wb') as f, patch.object(r, 'RAW_BYTES', 8192):
            self.assertEqual(r.stream_payload(self.root, self.m, f.fileno()), self.m['root_payload']['sha256'])
        self.assertEqual(dest.read_bytes(), self.raw)

    def test_raw_corruption_is_detected(self):
        self.raw[-1] = 1
        self.path.write_bytes(self.raw)
        with patch.object(r, 'RAW_BYTES', 8192), self.assertRaisesRegex(RuntimeError, 'checksum'):
            r.validate_payload(self.root, self.m)

    def test_wrong_format_or_path_is_refused(self):
        for key, value in [('encoding', 'unknown'), ('file', '../outside'), ('bytes', 8191), ('sha256', 'wrong')]:
            m = json.loads(json.dumps(self.m))
            m['root_payload'][key] = value
            with self.subTest(key=key), patch.object(r, 'RAW_BYTES', 8192), self.assertRaises(RuntimeError):
                r.payload_spec(m)

    def test_raw_requires_new_schema(self):
        self.m['schema'] = 1
        with patch.object(r, 'RAW_BYTES', 8192), self.assertRaises(RuntimeError):
            r.payload_spec(self.m)

    def test_wrong_root_uuid_is_refused(self):
        self.raw[1128:1144] = bytes(16)
        self.path.write_bytes(self.raw)
        with patch.object(r, 'RAW_BYTES', 8192), self.assertRaisesRegex(RuntimeError, 'UUID'):
            r.stream_payload(self.root, self.m)

    def test_disarm_removes_only_generated_boot_controls(self):
        for name in ('recovery-arm.json', 'recovery-release.json', 'AUTOMATIC-RESTORE.txt', 'Image'):
            (self.root / name).write_text('fixture')
        with patch.object(rolling.b, 'command'):
            rolling.disarm(self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Image', 'root.ext4'])

    def test_regular_file_guard_rejects_symlink(self):
        link = self.root / 'alias'
        link.symlink_to(self.path)
        with self.assertRaises(RuntimeError):
            rolling.require_regular(link)

    def test_generated_cleanup_rejects_escape(self):
        with self.assertRaises(RuntimeError):
            rolling.clear_child(self.root, '../outside')

    def test_incremental_copy_keeps_unchanged_files_and_updates_changes(self):
        source = self.root / 'source'
        destination = self.root / 'destination'
        source.mkdir()
        destination.mkdir()
        (source / 'unchanged').write_text('keep this file')
        changed = source / 'settings'
        changed.write_text('old')
        (source / 'removed').write_text('obsolete')
        log = self.root / 'copy.log'
        rolling.copy_system(source, destination, log)
        inode = (destination / 'unchanged').stat().st_ino
        changed.write_text('new configuration')
        (source / 'removed').unlink()
        rolling.copy_system(source, destination, log)
        self.assertEqual((destination / 'unchanged').stat().st_ino, inode)
        self.assertEqual((destination / 'settings').read_text(), 'new configuration')
        self.assertFalse((destination / 'removed').exists())
        self.assertIn('Number of regular files transferred: 1', log.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)

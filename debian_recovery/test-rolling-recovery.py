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
import nas_repack_image as repack


class CapacityTests(unittest.TestCase):
    def test_fresh_capacity_has_reserve_and_is_rounded(self):
        used = 13136920576
        size = repack.capacity(used)
        self.assertEqual(size % (512*1024**2), 0)
        self.assertGreater(size-used, 1024**3)
        self.assertLess(size, 24*1024**3)

    def test_owned_geometry_keeps_boot_offsets_and_rejects_unaligned_sizes(self):
        c = rolling.c
        original = c.USB_BYTES
        self.addCleanup(c.configure_layout, original)
        total = 1050624*512 + 15*1024**3
        c.configure_layout(total)
        self.assertEqual(c.USB_BYTES, total)
        self.assertEqual(c.USB_PARTS, [(2048,1048576),(1050624,15*1024**3//512)])
        for bad in (total+1, 1050624*512, 1050624*512+129*1024**3):
            with self.assertRaises(RuntimeError):
                c.configure_layout(bad)


class PhaseTimingTests(unittest.TestCase):
    def test_nested_phases_and_failure_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / 'reports').mkdir()
            with patch.object(rolling.time, 'monotonic', side_effect=[10, 11, 13, 17]):
                with self.assertRaisesRegex(RuntimeError, 'original failure'):
                    with rolling.timed(stage, 'outer'):
                        with rolling.timed(stage, 'inner'):
                            pass
                        raise RuntimeError('original failure')
            report = json.loads((stage / 'reports/phase-timings.json').read_text())
            self.assertEqual([(p['name'], p['seconds'], p['status']) for p in report['phases']],
                             [('inner', 2, 'OK'), ('outer', 7, 'FAILED')])
            self.assertFalse((stage / 'reports/phase-timings.pending').exists())

    def test_report_failure_does_not_mask_original_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'backup error'):
                with rolling.timed(Path(tmp), 'missing_reports_directory'):
                    raise ValueError('backup error')


class RuntimeRefreshTests(unittest.TestCase):
    def test_refresh_prunes_only_runtime_extras_and_preserves_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / 'reports').mkdir()
            source, root = stage / 'saved', stage / 'runtime'
            source.mkdir()
            root.mkdir()
            def write(base, name, value):
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            omitted = ('usr/src/kernel/Makefile', 'usr/share/doc/package/copyright',
                       'usr/share/man/man1/tool.1', 'var/cache/apt/pkgcache.bin')
            for name in omitted:
                write(source, name, 'saved original')
                write(root, name, 'old duplicate')
            kept = ('usr/bin/python3', 'usr/lib/modules/kernel/module.ko',
                    'etc/printer.cfg', 'home/cp/.ssh/authorized_keys')
            for name in kept:
                write(source, name, 'needed runtime input')
            write(source, 'home/cp/private-data', 'saved user data')
            write(source, 'root/private-data', 'saved root data')
            write(root, 'opt/chromapad-recovery/root.ext4', 'preserve payload')
            write(root, 'var/lib/chromapad-recovery/attempt.json', 'old attempt')
            write(root, 'obsolete', 'remove stale runtime file')
            rolling.runtime_copy(source, root, stage)
            for name in omitted:
                self.assertEqual((source / name).read_text(), 'saved original')
                self.assertFalse((root / name).exists())
            for name in kept:
                self.assertEqual((root / name).read_text(), 'needed runtime input')
            self.assertEqual((root / 'opt/chromapad-recovery/root.ext4').read_text(), 'preserve payload')
            self.assertFalse((root / 'home/cp/private-data').exists())
            self.assertFalse((root / 'root/private-data').exists())
            self.assertFalse((root / 'obsolete').exists())
            self.assertEqual(list((root / 'var/lib/chromapad-recovery').iterdir()), [])
            self.assertIn('Number of files:', (stage / 'reports/runtime-copy.txt').read_text())
            # Unchanged executable is reused on the next refresh.
            inode = (root / 'usr/bin/python3').stat().st_ino
            rolling.runtime_copy(source, root, stage)
            self.assertEqual((root / 'usr/bin/python3').stat().st_ino, inode)


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

    def test_codex_logs_and_cache_excluded_but_history_and_state_preserved(self):
        source, destination = self.root / 'source', self.root / 'destination'
        source.mkdir()
        destination.mkdir()
        codex = source / 'root/.codex'
        codex.mkdir(parents=True)
        transient = ('logs_2.sqlite', 'logs_2.sqlite-shm', 'logs_2.sqlite-wal',
                     'models_cache.json')
        preserved = ('state_5.sqlite', 'state_5.sqlite-wal', 'thread_history_1.sqlite',
                     'config.toml', 'sessions/session.jsonl', 'skills/example/SKILL.md')
        for name in transient + preserved:
            file = codex / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('original')
        rolling.copy_system(source, destination, self.root / 'copy.log')
        for name in transient:
            self.assertFalse((destination / 'root/.codex' / name).exists())
            self.assertEqual((codex / name).read_text(), 'original')
        for name in preserved:
            self.assertEqual((destination / 'root/.codex' / name).read_text(), 'original')
        # Background log/cache changes do not invalidate the consistency check.
        for name in transient:
            (codex / name).write_text('changed after copy')
        difference = rolling.b.run(*rolling.b.RSYNC, '--dry-run', '--itemize-changes',
                                   str(source) + '/', str(destination) + '/')
        self.assertEqual(difference, '')
        (codex / 'sessions/session.jsonl').write_text('new session data')
        difference = rolling.b.run(*rolling.b.RSYNC, '--dry-run', '--itemize-changes',
                                   str(source) + '/', str(destination) + '/')
        self.assertIn('sessions/session.jsonl', difference)

    def test_single_copy_preserves_metadata_links_and_full_comparison(self):
        source, destination = self.root / 'source', self.root / 'destination'
        source.mkdir()
        destination.mkdir()
        (destination / 'obsolete').write_text('delete this file')
        (destination / 'config').write_text('previous contents')
        config = source / 'config'
        config.write_text('complete current configuration')
        config.chmod(0o640)
        os.setxattr(config, 'user.single-copy-fixture', b'preserved')
        os.link(config, source / 'hardlink')
        (source / 'symlink').symlink_to('config')
        rolling.copy_system(source, destination, self.root / 'copy.log')
        self.assertEqual((destination / 'config').read_bytes(), config.read_bytes())
        self.assertEqual((destination / 'config').stat().st_mode & 0o777, 0o640)
        self.assertEqual(os.getxattr(destination / 'config', 'user.single-copy-fixture'), b'preserved')
        self.assertTrue(os.path.samefile(destination / 'config', destination / 'hardlink'))
        self.assertEqual(os.readlink(destination / 'symlink'), 'config')
        self.assertFalse((destination / 'obsolete').exists())
        def comparison():
            difference = rolling.b.run(*rolling.b.RSYNC, '--dry-run', '--itemize-changes',
                                       str(source) + '/', str(destination) + '/')
            return rolling.b.comparison_result(difference)
        self.assertEqual(comparison()['blocking'], [])
        # The final comparison still rejects source metadata drift after copying.
        config.chmod(0o600)
        self.assertTrue(comparison()['blocking'])

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

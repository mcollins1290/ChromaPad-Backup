import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import recovery_artifacts as a
from package_recovery_runtime import package


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(a, 'CACHE', self.root / 'cache')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def job(self, name):
        stage = self.root / name
        (stage / 'reports').mkdir(parents=True)
        return stage, a.ArtifactCache(stage)

    def test_unvalidated_build_is_not_reused_and_verified_build_is(self):
        stage, cache = self.job('first')
        def build():
            (stage / 'artifact').write_bytes(b'good artifact')
            return {'files': 1, 'elf_files': 1, 'uncompressed_file_bytes': 1}
        cache.get('runtime', {'input': 1}, stage / 'artifact', build)
        second, other = self.job('before_validation')
        with self.assertRaisesRegex(RuntimeError, 'must build'):
            other.get('runtime', {'input': 1}, second / 'artifact', lambda: (_ for _ in ()).throw(RuntimeError('must build')))
        a.publish_artifacts(stage)
        third, warm = self.job('verified')
        result = warm.get('runtime', {'input': 1}, third / 'artifact', lambda: self.fail('Cache should hit'))
        self.assertEqual(result, {'files': 1, 'elf_files': 1, 'uncompressed_file_bytes': 1})
        self.assertEqual((third / 'artifact').read_bytes(), b'good artifact')
        self.assertEqual(warm.events[-1]['status'], 'HIT')
        self.assertFalse((third / 'artifact-cache-pending.json').exists())

    def test_changed_inputs_or_corrupt_cache_rebuild(self):
        stage, cache = self.job('first')
        cache.get('initramfs', {'input': 1}, stage / 'artifact', lambda: ((stage/'artifact').write_bytes(b'good') and {}))
        a.publish_artifacts(stage)
        for name, inputs, corrupt in [('changed', {'input': 2}, False), ('corrupt', {'input': 1}, True)]:
            if corrupt:
                (a.CACHE / 'initramfs/artifact.bin').write_bytes(b'bad')
            job, new = self.job(name)
            def build():
                (job / 'artifact').write_bytes(b'new')
                return {}
            new.get('initramfs', inputs, job / 'artifact', build)
            self.assertEqual(new.events[-1]['status'], 'BUILT')
            self.assertEqual((job / 'artifact').read_bytes(), b'new')

    def test_dependency_contents_modes_membership_and_absolute_links(self):
        root = self.root / 'runtime'
        (root / 'usr/lib').mkdir(parents=True)
        file = root / 'usr/lib/library'
        file.write_text('one')
        (root / 'alias').symlink_to('/usr/lib/library')
        old = a.root_records(root, ['alias'])
        self.assertEqual(old['files']['usr/lib/library']['sha256'], hashlib.sha256(b'one').hexdigest())
        os.utime(file, (1, 1))
        self.assertEqual(old, a.root_records(root, ['alias']))
        file.write_text('two')
        self.assertNotEqual(old, a.root_records(root, ['alias']))
        content = a.root_records(root, ['alias'])
        file.chmod(0o600)
        self.assertNotEqual(content, a.root_records(root, ['alias']))
        self.assertNotEqual(a.root_records(root, ['alias']), a.root_records(root, ['usr/lib/library']))
        (root / 'bad').symlink_to('../../outside')
        with self.assertRaises(RuntimeError):
            a.root_records(root, ['bad'])

    def test_runtime_plan_detects_new_files_and_changed_library_precedence(self):
        root = self.root / 'runtime'
        names = ['usr/bin/'+n for n in ('python3','mount','umount','findmnt','udevadm','systemctl','sync','true','env')]
        names += ['usr/sbin/'+n for n in ('blkid','blockdev','e2fsck','fsck.vfat')]
        names += ['usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'usr/lib/python3.13/os.py',
                  'usr/lib/python3/dist-packages/PIL/Image.py', 'usr/lib/libexample.so.1']
        names += ['etc/'+n for n in ('passwd','group','nsswitch.conf','hosts')]
        for name in names:
            file = root/name
            file.parent.mkdir(parents=True,exist_ok=True)
            file.write_text('fixture '+name)
        (root/'usr/bin/python3').write_bytes(b'\x7fELFfixture')
        engine = self.root/'engine.py'
        engine.write_text('engine')
        engine.with_name('recovery_ui.py').write_text('display')
        def run_job(name):
            stage, cache = self.job(name)
            with patch('package_recovery_runtime.subprocess.check_output', return_value='Shared library: [libexample.so.1]'):
                package(root,stage,engine,cache=cache)
            return stage, cache
        first, cache = run_job('initial')
        a.publish_artifacts(first)
        _, same = run_job('same')
        self.assertEqual(same.events[-1]['status'], 'HIT')
        preferred=root/'usr/lib/aarch64-linux-gnu/libexample.so.1'
        preferred.parent.mkdir()
        preferred.write_text('preferred library')
        changed, choice = run_job('new_library')
        self.assertEqual(choice.events[-1]['status'], 'BUILT')
        a.publish_artifacts(changed)
        (root/'usr/lib/python3.13/new_module.py').write_text('new standard library module')
        _, added = run_job('new_module')
        self.assertEqual(added.events[-1]['status'], 'BUILT')

    def test_unsafe_cache_is_not_followed(self):
        other = self.root / 'other'
        other.mkdir()
        a.CACHE.symlink_to(other)
        stage, cache = self.job('first')
        def build():
            (stage / 'artifact').write_bytes(b'good')
            return {}
        cache.get('runtime', {}, stage / 'artifact', build)
        a.publish_artifacts(stage)
        self.assertEqual(list(other.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)

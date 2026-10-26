#!/usr/bin/python3
"""Explicit integration test: two updates and restore using disposable files only."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
import chromapad_recovery as r
import recovery_build_common as c
import rolling_recovery as rolling
from recovery_build_common import b


def main():
    b.require(os.geteuid() == 0, 'This file-backed integration test needs root for loop mounts')
    with tempfile.TemporaryDirectory(prefix='chromapad-rolling-fixture-', dir='/var/tmp') as temp:
        stage = Path(temp)
        (stage / 'reports').mkdir()
        image = stage / 'only-recovery.img'
        with image.open('xb') as f:
            f.truncate(c.USB_BYTES)
        inode = image.stat().st_ino
        m = rolling.metadata()
        rolling.initialize(image, m)
        size = 64 * 1024 ** 2
        with patch.object(r, 'RAW_BYTES', size):
            for iteration in (1, 2):
                with c.usb_image(stage, True, image=image) as (fat, root):
                    payload = root / 'saved.ext4'
                    if iteration == 1:
                        with payload.open('xb') as f:
                            f.truncate(size)
                        b.command('mkfs.ext4', '-q', '-F', '-U', b.ROOT_UUID, payload)
                    with rolling.mounted_payload(payload, stage) as saved:
                        file = saved / 'settings'
                        file.write_text('configuration version ' + str(iteration))
                        file.chmod(0o640)
                        os.chown(file, 1000, 1000)
                        os.setxattr(file, 'user.backup-fixture', b'preserved')
                        if iteration == 1:
                            (saved / 'alias').symlink_to('settings')
                            os.link(file, saved / 'hardlink')
                            (saved / 'remove-on-update').write_text('old file')
                        else:
                            (saved / 'remove-on-update').unlink()
                        b.command('fstrim', saved)
                    checksum = rolling.hash_payload(payload)
                    manifest = {'schema': 2, 'root_payload': {'file': 'root.ext4', 'encoding': 'raw',
                                'bytes': size, 'raw_bytes': size, 'sha256': checksum, 'raw_sha256': checksum}}
                    # Use the same filename expected by the actual restore engine.
                    payload.rename(root / 'root.ext4')
                    restored = stage / 'disposable-restored.ext4'
                    with restored.open('wb') as f:
                        r.stream_payload(root, manifest, f.fileno())
                        f.flush()
                        os.fsync(f.fileno())
                    (root / 'root.ext4').rename(payload)
                    b.command('e2fsck', '-f', '-n', restored)
                    loop = b.run('losetup', '--find', '--show', '--read-only', restored)
                    point = stage / 'restored'
                    point.mkdir(exist_ok=True)
                    mounts = []
                    try:
                        b.command('mount', '-t', 'ext4', '-o', 'ro,noload', loop, point)
                        mounts.append((loop, point))
                        file = point / 'settings'
                        assert file.read_text() == 'configuration version ' + str(iteration)
                        assert file.stat().st_mode & 0o777 == 0o640 and file.stat().st_uid == 1000
                        assert os.getxattr(file, 'user.backup-fixture') == b'preserved'
                        assert (point / 'alias').read_text() == file.read_text()
                        assert os.path.samefile(file, point / 'hardlink')
                        assert (point / 'remove-on-update').exists() == (iteration == 1)
                    finally:
                        b.cleanup_image(restored, [loop], mounts)
                    restored.unlink()
                assert image.stat().st_ino == inode
                assert len(list(stage.glob('*.img'))) == 1
        rolling.check_filesystems(image, stage)
        assert not b.run('losetup', '-j', image)
        print('PASS: two in-place updates, same image inode, exact restores, metadata, links, xattrs and cleanup')


if __name__ == '__main__':
    main()

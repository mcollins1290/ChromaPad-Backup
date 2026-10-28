"""Recreate a smaller container from a READY snapshot; never shrink a filesystem."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import tempfile
import nas_payload_hash as h

CANDIDATE = h.IMAGE.with_name('.coprint-debian.rebuild.img')
QUANTUM = 512 * 1024 ** 2


def capacity(used):
    # Keep boot-backup/log space, growth allowance and new-filesystem metadata.
    reserve = max(1024 ** 3, (used * 15 + 99) // 100)
    return ((used + reserve + 256 * 1024 ** 2 + QUANTUM - 1) // QUANTUM) * QUANTUM


def command(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


@contextlib.contextmanager
def mounted(image, start, sectors, uuid, writable=False):
    point = Path(tempfile.mkdtemp(prefix='chromapad-repack-'))
    loop = None
    active = False
    try:
        args = ['losetup', '--find', '--show', '--offset', start * 512, '--sizelimit', sectors * 512]
        if not writable:
            args.append('--read-only')
        loop = h.run(*args, image)
        h.require(h.run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong repack loop')
        if writable:
            command('mkfs.ext4', '-F', '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0',
                    '-U', uuid, '-L', 'CP_RECOVERY', '-m', '1', loop, stdout=subprocess.DEVNULL)
        h.require(h.run('blkid', '-p', '-c', '/dev/null', '-s', 'UUID', '-o', 'value', loop) == uuid,
                  'Wrong repack filesystem UUID')
        command('mount', '-t', 'ext4', '-o',
                'rw,noatime,nosuid,nodev,noexec' if writable else 'ro,noload,nosuid,nodev,noexec', loop, point)
        active = True
        yield point
    finally:
        if active:
            command('umount', point)
        if loop is not None:
            command('losetup', '-d', loop)
        point.rmdir()


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def repack(request):
    h.require(os.geteuid() == 0, 'NAS recreation requires root')
    with h.LOCK.open('a') as lock:
        h.fcntl.flock(lock, h.fcntl.LOCK_EX | h.fcntl.LOCK_NB)
        before = h.image_identity(h.IMAGE, request)
        h.require(not h.run('losetup', '-j', h.IMAGE), 'Source image in use on NAS')
        with mounted(h.IMAGE, request['start_sector'], request['sectors'], request['usb_root_uuid']) as source:
            used = shutil.disk_usage(source).used
            root_bytes = capacity(used)
            total = request['start_sector'] * 512 + root_bytes
            result = {'source_used_bytes': used, 'reserve_policy': '15 percent or 1 GiB, plus 256 MiB metadata, rounded to 512 MiB',
                      'image_bytes': total, 'root_sectors': root_bytes // 512}
            if request['operation'] == 'plan':
                return result
            h.require(request['operation'] == 'repack' and total == request['new_image_bytes'],
                      'Image sizing changed; run plan again')
            h.require(total < request['image_bytes'], 'Recreation would not reduce the image')
            h.require(not CANDIDATE.exists() and not CANDIDATE.is_symlink(), 'Rebuild candidate already exists')
            h.require(shutil.disk_usage(h.IMAGE.parent).free > total + 1024 ** 3, 'Insufficient NAS workspace')
            h.verify_package_metadata(source / 'opt/chromapad-recovery/root.ext4', request)
            with h.IMAGE.open('rb') as src, CANDIDATE.open('xb') as dst:
                os.fchmod(dst.fileno(), 0o600)
                os.fchown(dst.fileno(), before.st_uid, before.st_gid)
                prefix = bytearray(src.read(1024 ** 2))
                struct.pack_into('<II', prefix, 470, request['start_sector'], root_bytes // 512)
                dst.truncate(total)
                dst.write(prefix)
                # Keep the verified boot partition byte-for-byte, including its arm token.
                src.seek(2048 * 512)
                dst.seek(2048 * 512)
                remaining = 1048576 * 512
                while remaining:
                    block = src.read(min(4 * 1024 ** 2, remaining))
                    h.require(bool(block), 'Short source boot partition')
                    dst.write(block)
                    remaining -= len(block)
                dst.flush()
                os.fsync(dst.fileno())
            with mounted(CANDIDATE, request['start_sector'], root_bytes // 512,
                         request['usb_root_uuid'], writable=True) as target:
                print('REPACK copying the verified snapshot on NAS', flush=True)
                command('rsync', '-aHAXxS', '--numeric-ids', '--exclude=/lost+found',
                        str(source) + '/', str(target) + '/')
                command('sync', '-f', target)
                print('REPACK checking all copied contents and metadata', flush=True)
                difference = h.run('rsync', '-aHAXxnc', '--numeric-ids', '--delete', '--itemize-changes',
                                   '--exclude=/lost+found', str(source) + '/', str(target) + '/')
                h.require(not difference, 'Recreated filesystem differs: ' + difference[:2000])
                bundle = target / 'opt/chromapad-recovery'
                h.require(file_sha(bundle / 'manifest.json') == request['manifest_sha256'], 'Recreated manifest differs')
                payload = h.hash_file(bundle / 'root.ext4', request)
                h.require(payload['sha256'] == request['root_sha256'], 'Recreated payload checksum differs')
                h.require(shutil.disk_usage(target).free > 1024 ** 3, 'Recreated image lacks free-space reserve')
            loop = h.run('losetup', '--find', '--show', '--read-only', '--offset',
                         request['start_sector'] * 512, '--sizelimit', root_bytes, CANDIDATE)
            try:
                command('e2fsck', '-f', '-n', loop)
            finally:
                command('losetup', '-d', loop)
            after = h.image_identity(h.IMAGE, request)
            h.require((before.st_mtime_ns, before.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns),
                      'Source image changed during recreation')
            result.update(candidate_inode=CANDIDATE.stat().st_ino, root_sha256=payload['sha256'],
                          candidate_mtime_ns=CANDIDATE.stat().st_mtime_ns,
                          manifest_sha256=request['manifest_sha256'], copied_contents_verified=True)
        h.require(not h.run('losetup', '-j', h.IMAGE) and not h.run('losetup', '-j', CANDIDATE),
                  'Repack loops remain')
        result['cleanup_complete'] = True
        return result


if __name__ == '__main__':
    os.umask(0o077)
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    def interrupted(number, frame):
        raise RuntimeError('NAS recreation interrupted by signal ' + str(number))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    print('REPACK_RESULT ' + json.dumps(repack(REQUEST)), flush=True)

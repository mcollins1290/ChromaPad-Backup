"""Self-contained helper sent over SSH; mounts only the configured image read-only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time
import uuid

IMAGE = Path('/media/raid1/coprint/coprint-debian.img')
LOCK = Path('/run/lock/chromapad-payload-hash.lock')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*args):
    return subprocess.check_output([str(x) for x in args], text=True).strip()


def image_identity(image, request):
    info = image.lstat()
    require(image == IMAGE and stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            'Not the configured regular NAS image')
    require(info.st_size == request['image_bytes'] and info.st_ino == request['image_inode'],
            'NAS image identity differs from the pad')
    with image.open('rb') as stream:
        mbr = stream.read(512)
    require(mbr[510:] == b'\x55\xaa' and mbr[478:510] == bytes(32), 'Invalid NAS image MBR')
    require(format(struct.unpack_from('<I', mbr, 440)[0], '08x') == request['usb_disk_id'],
            'NAS image disk ID differs')
    require(struct.unpack_from('<II', mbr, 470) == (request['start_sector'], request['sectors']),
            'NAS recovery partition differs')
    return info


def check_superblock(data, request):
    require(len(data) >= 2048, 'Missing payload superblock')
    require(struct.unpack_from('<H', data, 1080)[0] == 0xef53, 'Payload is not ext4')
    require(str(uuid.UUID(bytes=data[1128:1144])) == request['root_uuid'], 'Payload UUID differs')
    require(struct.unpack_from('<H', data, 1082)[0] & 1, 'Payload filesystem is not clean')
    blocks = struct.unpack_from('<I', data, 1028)[0]
    if struct.unpack_from('<I', data, 1120)[0] & 0x80:
        blocks |= struct.unpack_from('<I', data, 1360)[0] << 32
    require(blocks * (1024 << struct.unpack_from('<I', data, 1048)[0]) == request['raw_bytes'],
            'Payload filesystem geometry differs')


def hash_file(payload, request):
    info = payload.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_size == request['raw_bytes'], 'Invalid NAS payload file')
    value = hashlib.sha256()
    done, next_report = 0, 1024 ** 3
    start, cpu = time.monotonic(), time.process_time()
    with payload.open('rb', buffering=0) as stream:
        while block := stream.read(4 * 1024 ** 2):
            if done == 0:
                check_superblock(block, request)
            value.update(block)
            done += len(block)
            require(done <= request['raw_bytes'], 'Payload grew during hashing')
            if done >= next_report:
                print(f'Checking saved root on NAS: {done // 1024 ** 2} MiB', flush=True)
                next_report += 1024 ** 3
    after = payload.stat()
    require((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'Payload changed during hashing')
    require(done == request['raw_bytes'], 'Short NAS payload read')
    return {'schema': 1, 'location': 'NAS', 'bytes': done, 'sha256': value.hexdigest(),
            'root_uuid': request['root_uuid'], 'usb_root_uuid': request['usb_root_uuid'],
            'seconds': round(time.monotonic() - start, 3),
            'cpu_seconds': round(time.process_time() - cpu, 3)}


def hash_image(request):
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = image_identity(IMAGE, request)
        require(not run('losetup', '-j', IMAGE), 'NAS image already has loop devices')
        point = Path(tempfile.mkdtemp(prefix='chromapad-nas-hash-'))
        loop, mounted = None, False
        try:
            loop = run('losetup', '--find', '--show', '--read-only', '--offset',
                       request['start_sector'] * 512, '--sizelimit', request['sectors'] * 512, IMAGE)
            require(run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(IMAGE), 'Wrong NAS loop backing file')
            require(run('blkid', '-p', '-c', '/dev/null', '-s', 'UUID', '-o', 'value', loop) ==
                    request['usb_root_uuid'], 'Wrong NAS recovery filesystem UUID')
            subprocess.run(['mount', '-t', 'ext4', '-o', 'ro,noload,nosuid,nodev,noexec', loop, str(point)], check=True)
            mounted = True
            bundle = point / 'opt/chromapad-recovery'
            require(bundle.resolve() == bundle, 'Symlinked NAS payload directory')
            result = hash_file(bundle / 'root.ext4', request)
            after = image_identity(IMAGE, request)
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'NAS image changed during hashing')
        finally:
            # A failed unmount must fail the job and retain the diagnostic path.
            if mounted:
                subprocess.run(['umount', str(point)], check=True)
            if loop:
                subprocess.run(['losetup', '-d', loop], check=True)
            point.rmdir()
        result['cleanup_complete'] = True
        return result


def main(request):
    require(os.geteuid() == 0, 'NAS helper requires root')
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    for name in ('losetup', 'blkid', 'mount', 'umount', 'unshare'):
        require(shutil.which(name), 'Missing NAS tool: ' + name)
    def interrupted(number, frame):
        raise RuntimeError('NAS checksum interrupted by signal ' + str(number))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    if request['operation'] == 'probe':
        with LOCK.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require(not run('losetup', '-j', IMAGE), 'NAS image already has loop devices')
        result = {'schema': 1, 'ready': True, 'location': 'NAS'}
    else:
        require(request['operation'] == 'hash', 'Unknown NAS operation')
        result = hash_image(request)
    # Never publish a successful digest until all remote mounts/loops are gone.
    print('NAS_HASH_RESULT ' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main(REQUEST)

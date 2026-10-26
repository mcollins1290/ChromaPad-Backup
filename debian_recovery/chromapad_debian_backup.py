#!/usr/bin/env python3
"""Create and validate a separate Debian eMMC recovery-source image.

No physical-device writes, reboot, vendor recovery execution or stock-image update.
Default is a read-only plan. --capture creates a new private backup directory.
The resulting image is an eMMC-layout archive, NOT an automatic USB installer.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib

BASE = Path('/media/raid1/coprint_2_debian/backups')
STOCK = Path('/media/raid1/coprint')
CID = '150100414a4e4234520735a5fac66300'
ROOT_UUID = '89ebf872-79e9-488b-84d7-5ffb49263c0e'
FAT_UUID = 'B1C3-F827'
SIZE = 15634268160
PARTS = [(139264, 1046528, 'c'), (3289088, 27246592, '83')]
RANGES = [(0, 71303168), (607125504, 1684013056)]
MBR_SHA = 'e7ac296d23ef572ae908c377af0289ef0a7a355bd4603e0fd66e85d1fee2b60b'
USB_ARGS = ['usbcore.autosuspend=-1', 'usbcore.usbfs_memory_mb=256',
            'usbcore.old_scheme_first=1', 'usbcore.quirks=0781:5581:k',
            'usb-storage.delay_use=5', 'usb-storage.quirks=0781:5581:um']
EXCLUDES = ['/dev/*', '/proc/*', '/sys/*', '/run/*', '/tmp/*', '/mnt/*',
            '/media/*', '/boot/emmc/*', '/var/tmp/*', '/var/log/*', '/lost+found',
            '/root/.codex/logs_*.sqlite*', '/root/.codex/models_cache.json']
RSYNC = ['rsync', '-aHAXx', '--numeric-ids', '--delete'] + ['--exclude='+x for x in EXCLUDES]
VOLATILE_DIRS = {'dev/', 'proc/', 'sys/', 'run/', 'tmp/', 'mnt/', 'media/',
                 'boot/emmc/', 'var/tmp/', 'var/log/'}


def require(test, message):
    if not test:
        raise RuntimeError(message)


def run(*args, timeout=None):
    return subprocess.check_output([str(a) for a in args], text=True, timeout=timeout).strip()


def command(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def comparison_result(difference):
    """Ignore only mtime drift on directories whose transient contents we exclude."""
    blocking, ignored = [], []
    for line in difference.splitlines():
        if not line:
            continue
        if line[:12] == '.d..t...... ' and line[12:] in VOLATILE_DIRS:
            ignored.append(line)
        else:
            blocking.append(line)
    return {'blocking': blocking, 'ignored_volatile_directory_mtimes': ignored}


def digest(path, offset=0, length=None):
    h = hashlib.sha256()
    with Path(path).open('rb', buffering=0) as f:
        f.seek(offset)
        while length is None or length > 0:
            b = f.read(4*1024*1024 if length is None else min(length, 4*1024*1024))
            if not b:
                require(length is None, 'Short read: '+str(path))
                break
            h.update(b)
            if length is not None:
                length -= len(b)
    return h.hexdigest()


def validate_mbr(data):
    require(len(data) == 512 and hashlib.sha256(data).hexdigest() == MBR_SHA, 'Unexpected MBR')
    require(data[510:] == b'\x55\xaa', 'Missing MBR signature')
    actual = [(struct.unpack_from('<II', data, 446+i*16+8) + (format(data[446+i*16+4], 'x'),)) for i in range(2)]
    require(actual == PARTS, 'Unexpected partition geometry')
    require(not any(data[478:510]), 'Unexpected extra partitions')


def check_destination(base):
    require(base == BASE and base.resolve() == base, 'Only the dedicated Debian backup directory is allowed')
    require(not base.is_symlink() and not base.resolve().is_relative_to(STOCK.resolve()), 'Unsafe output path')
    for parent in [base.parent, *base.parents]:
        require(not parent.is_symlink(), 'Symlinked output ancestor')
    free = shutil.disk_usage(base.parent).free  # Trigger the configured automount.
    mounts = json.loads(run('findmnt', '-J', '-T', base.parent))['filesystems']
    actual = [m for m in mounts if m['fstype'] != 'autofs']
    require(len(actual) == 1 and actual[0]['fstype'] in ('nfs', 'nfs4') and actual[0]['source'] == '192.168.0.2:/media/raid1', 'Expected NAS mount is absent')
    require(free > SIZE*2, 'Less than two image sizes of free NAS space')


def stock_reference():
    anchor = STOCK/'coprint.img'
    target = anchor.resolve(strict=True)
    s = target.stat()
    return {'anchor': str(anchor), 'link': os.readlink(anchor) if anchor.is_symlink() else None,
            'target': str(target), 'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns,
            'mbr_sha256': digest(target, 0, 512)}


def owner_access(directory):
    """Keep private modes but let the existing stock-backup owner read via SMB."""
    require(directory.parent == BASE and directory.is_dir() and not directory.is_symlink(), 'Unsafe publication directory')
    owner = (STOCK/'coprint.img').resolve(strict=True).stat()
    for p in directory.rglob('*'):
        os.chown(p, owner.st_uid, owner.st_gid, follow_symlinks=False)
    os.chown(directory, owner.st_uid, owner.st_gid)
    os.chown(BASE, owner.st_uid, owner.st_gid)
    return {'uid': owner.st_uid, 'gid': owner.st_gid}


def source_identity(printers_stopped=True):
    require(os.geteuid() == 0, 'Run as root')
    require(Path('/etc/chromapad-emmc').is_file(), 'Not the installed Debian eMMC system')
    devices = [p for p in Path('/sys/block').glob('mmcblk*') if (p/'device/cid').is_file() and (p/'device/cid').read_text().strip() == CID]
    require(len(devices) == 1, 'Pinned eMMC missing or ambiguous')
    p = devices[0]
    require('ffe07000.mmc' in str((p/'device').resolve()), 'Wrong controller')
    disk = Path('/dev')/p.name
    require(run('findmnt', '-n', '-o', 'SOURCE', '--mountpoint', '/') == str(disk)+'p2', 'Root is not the pinned eMMC')
    require(run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/') == ROOT_UUID, 'Wrong root UUID')
    require(run('findmnt', '-n', '-o', 'SOURCE', '--mountpoint', '/boot/emmc') == str(disk)+'p1', 'Wrong FAT source')
    require(run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/boot/emmc') == FAT_UUID, 'Wrong FAT UUID')
    require(run('blockdev', '--getsize64', disk) == str(SIZE), 'Wrong eMMC capacity')
    with disk.open('rb') as f:
        validate_mbr(f.read(512))
    for suffix in ('boot0', 'boot1'):
        require((Path('/sys/class/block')/(p.name+suffix)/'ro').read_text().strip() == '1', 'Hardware boot area not read-only')
    for i in (range(1, 9) if printers_stopped else ()):
        r = subprocess.run(['systemctl', 'is-active', 'klipper-'+str(i)+'.service'], capture_output=True, text=True)
        require(r.stdout.strip() == 'inactive', 'Stop printer services before backup')
    args = Path('/proc/cmdline').read_text().strip()
    require(all(a in args.split() for a in USB_ARGS), 'USB policy missing')
    return {'disk': str(disk), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'cid': CID, 'root_uuid': ROOT_UUID, 'fat_uuid': FAT_UUID, 'cmdline': args,
            'kernel': run('uname', '-r'), 'boot_entry_sha256': digest('/boot/emmc/emmc_autoscript')}


def unmount_image_point(point, attempts=16):
    """Allow terminating copy children time to release a mount; never force it."""
    for attempt in range(attempts):
        try:
            command('umount', point, timeout=5)
            return
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 32 or attempt == attempts-1:
                raise
            time.sleep(1)


def cleanup_image(image, loops, mounts):
    """Clean independent partitions even if one stays busy; retain its loop."""
    errors, busy = [], set()
    for loop, point in reversed(mounts):
        try:
            unmount_image_point(point)
        except Exception as exc:
            busy.add(loop)
            errors.append(str(point)+': '+str(exc))
    for loop in reversed(loops):
        if loop in busy:
            continue
        try:
            require(re.fullmatch(r'/dev/loop[0-9]+', loop), 'Unexpected cleanup loop')
            require(run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Cleanup loop backing file changed')
            command('losetup', '-d', loop)
        except Exception as exc:
            errors.append(loop+': '+str(exc))
    require(not errors, 'Image cleanup incomplete: '+'; '.join(errors))


@contextlib.contextmanager
def mounted_image(image, work, writable=False):
    require(image.is_file() and not image.is_symlink(), 'Image must be a regular file, not a symlink')
    require(stat.S_ISREG(image.stat().st_mode) and image.stat().st_size == SIZE, 'Wrong image type or size')
    loops, mounts = [], []
    try:
        points = []
        for index, (start, sectors, kind) in enumerate(PARTS):
            args = ['losetup', '--find', '--show', '--offset', start*512, '--sizelimit', sectors*512]
            if not writable:
                args.append('--read-only')
            loop = run(*args, image)
            loops.append(loop)
            require(re.fullmatch(r'/dev/loop[0-9]+', loop), 'Unexpected loop name')
            require(run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong loop backing file')
            point = work/('fat' if index == 0 else 'root')
            point.mkdir(exist_ok=True)
            opts = 'rw,noatime,nosuid,nodev' if writable else 'ro,noload,nosuid,nodev,noexec'
            if index == 0:
                opts = opts.replace(',noload', '')
            command('mount', '-t', 'vfat' if index == 0 else 'ext4', '-o', opts, loop, point)
            mounts.append((loop, point))
            points.append(point)
        yield points
    finally:
        cleanup_image(image, loops, mounts)


def format_partition(image, index):
    start, sectors, kind = PARTS[index]
    loop = run('losetup', '--find', '--show', '--offset', start*512, '--sizelimit', sectors*512, image)
    try:
        require(run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong format loop')
        if index == 0:
            command('mkfs.vfat', '-F', '32', '-i', FAT_UUID.replace('-', ''), '-n', 'BOOT', loop)
        else:
            command('mkfs.ext4', '-F', '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0', '-U', ROOT_UUID, '-L', 'CP_DEBIAN_EMMC', '-m', '1', loop)
    finally:
        command('losetup', '-d', loop)


def fsck_image(image, work):
    for i, (start, sectors, kind) in enumerate(PARTS):
        loop = run('losetup', '--find', '--show', '--read-only', '--offset', start*512, '--sizelimit', sectors*512, image)
        try:
            require(run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong fsck loop')
            with (work/('fsck-fat.txt' if i == 0 else 'fsck-root.txt')).open('w') as log:
                command(*(['fsck.vfat', '-n'] if i == 0 else ['e2fsck', '-f', '-n']), loop, stdout=log, stderr=subprocess.STDOUT)
        finally:
            command('losetup', '-d', loop)


@contextlib.contextmanager
def quiesced(work):
    timers = run('systemctl', 'list-units', '--type=timer', '--state=active', '--no-legend', '--plain').splitlines()
    units = [s.split()[0] for s in timers]
    units += ['ChromaScreen.service'] + ['moonraker-'+str(i)+'.service' for i in range(1, 9)]
    units += ['nginx.service', 'packagekit.service', 'cron.service']
    active = [u for u in units if subprocess.run(['systemctl', 'is-active', '--quiet', u]).returncode == 0]
    record(work/'quiesce.json', {'utc': stamp(), 'originally_active': active, 'restored': False})
    try:
        if active:
            command('systemctl', 'stop', *active, timeout=120)
        for unit in ['apt-daily.service', 'apt-daily-upgrade.service', 'dpkg-db-backup.service']:
            require(subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode != 0, 'Package job active: '+unit)
        with contextlib.ExitStack() as locks:
            for p in ['/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock']:
                f = locks.enter_context(open(p, 'a'))
                fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
    finally:
        # Restore only units active on entry. Never enable or start printer services.
        if active:
            command('systemctl', 'start', *active, timeout=120)
        record(work/'quiesce.json', {'utc': stamp(), 'originally_active': active, 'restored': True})


def validate_boot(fat, root):
    entry = (fat/'emmc_autoscript').read_bytes()
    require(len(entry) > 72, 'Short boot entry')
    header = bytearray(entry[:64]); header[4:8] = bytes(4)
    require(struct.unpack_from('>I', entry)[0] == 0x27051956 and entry[30] == 6, 'Invalid script header')
    require(zlib.crc32(header) == struct.unpack_from('>I', entry, 4)[0], 'Bad script header CRC')
    require(len(entry)-64 == struct.unpack_from('>I', entry, 12)[0], 'Bad script length')
    require(zlib.crc32(entry[64:]) == struct.unpack_from('>I', entry, 24)[0], 'Bad script data CRC')
    script = entry[72:].rstrip(b'\0').decode()
    lines = [s for s in script.splitlines() if s.startswith("setenv bootargs '")]
    require(len(lines) == 1, 'Ambiguous boot arguments')
    args = lines[0][17:-1].split()
    require('root=UUID='+ROOT_UUID in args, 'Wrong boot root')
    require([a for a in args if a.startswith('console=')] == ['console=ttyAML0,115200n8'], 'Unexpected console policy')
    require('fsck.repair=preen' in args and 'quiet' not in args and not any(a.startswith('systemd.show_status=') for a in args), 'Unexpected fsck/status policy')
    require(all(args.count(a) == 1 for a in USB_ARGS), 'Missing USB settings')
    gens = set(re.findall(r'debian/([A-Za-z0-9_-]+)/', script))
    require(len(gens) == 1, 'Ambiguous active generation')
    generation = gens.pop()
    manifest = json.loads((fat/'debian'/generation/'boot-manifest.json').read_text())
    for name, rec in manifest['files'].items():
        require(Path(name).name == name, 'Unsafe boot manifest filename')
        p = fat/'debian'/generation/name
        require(p.stat().st_size == rec['bytes'] and digest(p) == rec['sha256'], 'Boot asset changed: '+name)
    require(digest(fat/'emmc_autoscript') == digest(fat/'debian'/generation/'emmc_autoscript'), 'Active entry mismatch')
    require('UUID='+ROOT_UUID+' / ext4' in (root/'etc/fstab').read_text(), 'Wrong fstab root')
    require('UUID='+FAT_UUID+' /boot/emmc vfat' in (root/'etc/fstab').read_text(), 'Wrong fstab FAT')
    require('PS1=' in (root/'root/.bashrc').read_text(), 'Missing custom root shell')
    require((root/'etc/udev/rules.d/99-chromapad-usb-storage-no-media-poll.rules').is_file(), 'Missing storage polling rule')
    ids = json.loads((root/'usr/lib/chromapad/emmc/identifiers.json').read_text())
    require(ids['root_uuid'] == ROOT_UUID and ids['fat_uuid'] == FAT_UUID, 'Publisher identity mismatch')
    for name in ['vmlinuz-', 'initrd.img-']:
        require((root/'boot'/(name+ids['kernel'])).is_file(), 'Debian /boot missing')
    return {'generation': generation, 'bootargs': ' '.join(args), 'status': 'PASS'}


def copy_region(source, dest, start, end):
    with Path(source).open('rb', buffering=0) as src, Path(dest).open('r+b', buffering=0) as dst:
        src.seek(start); dst.seek(start)
        left = end-start
        while left:
            data = src.read(min(left, 4*1024*1024))
            require(bool(data), 'Short protected-region read')
            dst.write(data); left -= len(data)
        os.fsync(dst.fileno())


def phase(work, text):
    print(stamp(), text, flush=True)
    record(work/'status.json', {'utc': stamp(), 'phase': text, 'state': 'RUNNING'})


def capture():
    identity = source_identity()
    check_destination(BASE)
    stock_before = stock_reference()
    BASE.mkdir(mode=0o700, exist_ok=True)
    with (BASE/'.backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        generation = 'debian-emmc-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        work = Path(tempfile.mkdtemp(prefix='.incomplete-'+generation+'-', dir=BASE))
        print('BACKUP_WORK='+str(work), flush=True)
        image = work/'debian-emmc.img'
        try:
            with image.open('xb') as f:
                f.truncate(SIZE)
            record(work/'source.json', identity)
            record(work/'stock-before.json', stock_before)
            phase(work, 'Copying protected regions into the new regular-file image')
            protected = {}
            for start, end in RANGES:
                copy_region(identity['disk'], image, start, end)
                protected[str(start)+':'+str(end)] = digest(image, start, end-start)
                require(protected[str(start)+':'+str(end)] == digest(identity['disk'], start, end-start), 'Protected-region readback mismatch')
            for suffix in ('boot0', 'boot1'):
                with open(identity['disk']+suffix, 'rb') as src, (work/(suffix+'.bin')).open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024*1024)
            phase(work, 'Formatting only loop-backed image filesystems')
            format_partition(image, 0); format_partition(image, 1)
            with mounted_image(image, work, writable=True) as (fat, root):
                phase(work, 'Initial root copy while applications remain running')
                with (work/'rsync-initial.txt').open('w') as log:
                    p = subprocess.run(RSYNC+['--stats', '/', str(root)+'/'], stdout=log, stderr=subprocess.STDOUT)
                    require(p.returncode in (0, 24), 'Initial rsync failed')
                phase(work, 'Final application-consistent copy; temporarily stopping application writers')
                with quiesced(work):
                    with (work/'rsync-final.txt').open('w') as log:
                        command(*RSYNC, '--checksum', '--stats', '/', str(root)+'/', stdout=log, stderr=subprocess.STDOUT)
                    with (work/'rsync-fat.txt').open('w') as log:
                        command('rsync', '-rt', '--delete', '--modify-window=1', '/boot/emmc/', str(fat)+'/', stdout=log, stderr=subprocess.STDOUT)
                    difference = run(*RSYNC, '--checksum', '--dry-run', '--itemize-changes', '/', str(root)+'/')
                    (work/'root-comparison.txt').write_text(difference+'\n')
                    comparison = comparison_result(difference)
                    record(work/'root-comparison-policy.json', comparison)
                    require(not comparison['blocking'], 'Source changed during final copy; refusing publication')
                    require(not run('rsync', '-rcn', '--delete', '--itemize-changes', '/boot/emmc/', str(fat)+'/'), 'FAT copy differs')
                    boot = validate_boot(fat, root)
                    require(source_identity() == identity, 'Source boot or active boot entry changed')
                    command('sync', '-f', root); command('sync', '-f', fat)
                phase(work, 'Applications restored; unmounting and checking image filesystems')
            fsck_image(image, work)
            phase(work, 'Sealing complete image checksum')
            require(stock_reference() == stock_before, 'Stock anchor/reference changed')
            files = {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in [image, work/'boot0.bin', work/'boot1.bin']}
            manifest = {'schema': 1, 'kind': 'chromapad-debian-emmc-backup', 'created_utc': stamp(),
                        'generation': generation, 'source': identity, 'files': files, 'protected_regions': protected,
                        'boot': boot, 'excluded_contents': EXCLUDES, 'stock_unchanged': True,
                        'application_copy_verified': True, 'automatic_usb_recovery': False,
                        'physical_restore_tested': False}
            shutil.copy2(Path(__file__), work/'chromapad_debian_backup.py')
            record(work/'manifest.json', manifest)
            record(work/'status.json', {'state': 'VERIFIED', 'utc': stamp(), 'phase': 'Backup published; physical restore not tested'})
            dest = BASE/generation
            require(not dest.exists(), 'Backup generation already exists')
            command('sync', '-f', work)
            os.rename(work, dest)
            owner_access(dest)
            link = BASE/'debian-latest.img'
            require(not link.exists() or link.is_symlink(), 'Refusing to replace a non-symlink Debian anchor')
            pending = BASE/('.latest-'+generation)
            pending.symlink_to(generation+'/debian-emmc.img')
            os.replace(pending, link)
            command('sync', '-f', BASE)
            print(json.dumps({'status': 'BACKUP_PUBLISHED', 'directory': str(dest), 'image_sha256': files['debian-emmc.img']['sha256']}, indent=2), flush=True)
            return dest
        except BaseException as exc:
            if work.exists():
                record(work/'status.json', {'state': 'FAILED', 'utc': stamp(), 'error': str(exc), 'published': False})
            raise


def verify(directory):
    directory = directory.resolve(strict=True)
    manifest = json.loads((directory/'manifest.json').read_text())
    require(manifest['schema'] == 1 and manifest['kind'] == 'chromapad-debian-emmc-backup', 'Wrong backup format')
    require(manifest['source']['cid'] == CID, 'Wrong backup device')
    require(set(manifest['files']) == {'debian-emmc.img', 'boot0.bin', 'boot1.bin'}, 'Unexpected payload set')
    for name, rec in manifest['files'].items():
        p = directory/name
        require(p.is_file() and not p.is_symlink(), 'Unsafe backup payload')
        require(p.stat().st_size == rec['bytes'] and digest(p) == rec['sha256'], 'Checksum mismatch: '+name)
    image = directory/'debian-emmc.img'
    with image.open('rb') as f:
        validate_mbr(f.read(512))
    for start, end in RANGES:
        require(digest(image, start, end-start) == manifest['protected_regions'][str(start)+':'+str(end)], 'Protected range mismatch')
    with tempfile.TemporaryDirectory(prefix='chromapad-backup-verify-', dir='/var/tmp') as t:
        work = Path(t)
        fsck_image(image, work)
        with mounted_image(image, work) as (fat, root):
            require(validate_boot(fat, root) == manifest['boot'], 'Boot metadata mismatch')
    print(json.dumps({'status': 'BACKUP_VERIFIED', 'directory': str(directory), 'physical_restore_tested': False}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--capture', action='store_true')
    g.add_argument('--verify', type=Path)
    p.add_argument('--private', action='store_true', help=argparse.SUPPRESS)
    a = p.parse_args()
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    os.umask(0o077)
    if not a.capture and a.verify is None:
        check_destination(BASE)
        print(json.dumps({'status': 'PLAN_ONLY', 'source': source_identity(), 'output': str(BASE),
                          'stock_reference': stock_reference(), 'temporarily_stops_apps': True,
                          'image_type': 'eMMC-layout recovery source; not USB auto-installer', 'writes_performed': False}, indent=2))
        return
    require(os.geteuid() == 0, 'Root required')
    if not a.private:
        os.execvp('unshare', ['unshare', '--mount', '--propagation', 'private', sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], '--private'])
    def interrupted(signum, frame):
        raise RuntimeError('Interrupted by signal '+str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    capture() if a.capture else verify(a.verify)


if __name__ == '__main__':
    main()

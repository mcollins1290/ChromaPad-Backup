#!/usr/bin/python3
"""Update one Debian recovery image in place, preserving the stock fallback."""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import printer_lifecycle as printers
from promote_backup import anchor_identity, atomic_json, sync_dir

BUILDER = Path('/usr/local/lib/chromapad-recovery-builder')
DEST = Path('/media/raid1/coprint')
IMAGE = DEST / 'coprint-debian.img'
STATE = DEST / 'coprint-debian.json'
INFO = DEST / 'backup-info'
DEPENDENCIES = ('ssh', 'rsync', 'mkimage', 'fdtget', 'readelf', 'losetup', 'mount', 'umount',
                'unshare', 'cpio', 'gzip', 'fsck.vfat', 'e2fsck', 'mkfs.vfat',
                'mkfs.ext4', 'resize2fs', 'fstrim', 'blkid', 'findmnt', 'chroot', 'journalctl')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def managed_record(image, state, size):
    if not image.exists() and not image.is_symlink():
        require(not state.exists(), 'Image is missing but its ownership record remains; inspect before rebuilding')
        return None
    info = image.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == size,
            'Rolling target must be one regular image file of the expected size')
    require(state.is_file() and not state.is_symlink(), 'Existing rolling image has no ownership record')
    record = json.loads(state.read_text())
    require(record['kind'] == 'chromapad-rolling-image' and record['image'] == str(image)
            and record['inode'] == info.st_ino and record['bytes'] == size,
            'Rolling image ownership record does not match the target')
    return record


def required_space(image, size):
    # Reserve only possible growth of this one image plus compact work records.
    allocated = image.stat().st_blocks * 512 if image.exists() else 0
    return max(0, size - allocated) + 512 * 1024 ** 2


def load_builder():
    source = BUILDER.resolve(strict=True)
    sys.path.insert(0, str(source))
    import chromapad_recovery_backup as workflow
    import rolling_recovery as rolling
    workflow.verify_tools(source)
    if IMAGE.exists() or IMAGE.is_symlink():
        record = managed_record(IMAGE, STATE, IMAGE.lstat().st_size)
        rolling.c.configure_layout(record['bytes'])
    return rolling


def verify_nas_mount(mounts):
    actual = [item for item in mounts if item['fstype'] != 'autofs']
    require(len(actual) == 1 and actual[0]['fstype'] in ('nfs', 'nfs4')
            and actual[0]['source'] == '192.168.0.2:/media/raid1', 'Expected NAS mount is absent')


def readiness(rolling):
    require(os.geteuid() == 0, 'Run with sudo')
    require(DEST.resolve() == DEST and DEST.is_dir(), 'Expected NAS image directory is unavailable')
    mount = json.loads(rolling.b.run('findmnt', '-J', '-T', DEST))['filesystems']
    verify_nas_mount(mount)
    require(not INFO.is_symlink() and (not INFO.exists() or INFO.is_dir()), 'Unsafe backup report directory')
    missing = [name for name in DEPENDENCIES if not shutil.which(name)]
    require(not missing, 'Missing backup commands: ' + ', '.join(missing))
    record = managed_record(IMAGE, STATE, rolling.c.USB_BYTES)
    require(not rolling.b.run('losetup', '-j', IMAGE), 'Rolling image is already in use')
    before = anchor_identity(DEST)
    needed = required_space(IMAGE, rolling.c.USB_BYTES)
    require(shutil.disk_usage(DEST).free >= needed,
            f'Need {needed / 1024**3:.2f} GiB free for growth of the rolling image and reports')
    require(shutil.disk_usage('/var/tmp').free >= 1536 * 1024 ** 2,
            'Need 1.5 GiB free on the pad for disposable boot/runtime build files')
    old = rolling.b.run('systemctl', 'show', 'chromapad-debian-recovery-backup.service', '-p', 'ActiveState', '--value')
    require(old not in ('active', 'activating', 'deactivating'), 'The older backup service is running')
    rolling.b.source_identity(printers_stopped=False)
    source_metadata = rolling.nas.package_metadata(Path('/'))
    rolling.nas_preflight()
    checked = printers.plan()
    return {'status': 'PLAN_ONLY', 'image': str(IMAGE), 'destination': str(DEST / 'coprint.img'),
            'mode': 'UPDATE_IN_PLACE' if record else 'CREATE_FIRST_DEBIAN_IMAGE',
            'nas_free_required_bytes': needed, 'pad_scratch_required_bytes': 1536 * 1024 ** 2,
            'dated_images_created': False, 'stock_fallback_preserved': True,
            'source_package_metadata_files': len(source_metadata),
            'image_state': record.get('state') if record else 'NOT_CREATED',
            'requires_source_content_checksums': needs_content_check(record),
            'writes_performed': False, 'previous': before, **checked}, record


def save_state(record, owner):
    atomic_json(STATE, record)
    os.chown(STATE, owner.st_uid, owner.st_gid)
    STATE.chmod(0o600)


def compact_reports(stage, owner):
    INFO.mkdir(mode=0o700, exist_ok=True)
    require(not INFO.is_symlink(), 'Unsafe report directory')
    # Fixed names are replaced each run; each report has a bounded retained size.
    for source in (stage / 'reports').iterdir():
        if not source.is_file() or source.is_symlink():
            continue
        with source.open('rb') as f:
            if source.stat().st_size > 512 * 1024:
                f.seek(-512 * 1024, 2)
            content = f.read()
        target = INFO / source.name
        require(not target.is_symlink(), 'Unsafe report file')
        target.write_bytes(content)
        target.chmod(0o600)
        os.chown(target, owner.st_uid, owner.st_gid)
    # The preliminary copy was removed; do not present its older report as current.
    obsolete = INFO / 'rsync-initial.txt'
    require(not obsolete.is_symlink(), 'Unsafe obsolete backup report')
    obsolete.unlink(missing_ok=True)
    os.chown(INFO, owner.st_uid, owner.st_gid)


def activate(before, image):
    require(anchor_identity(DEST) == before, 'coprint.img changed during the backup')
    if (DEST / 'coprint.img').resolve() == image:
        return  # Subsequent runs retain both the image inode and current link.
    require(before['link'] is not None, 'Preserve/rename the regular stock anchor before first conversion')
    pending = DEST / '.coprint-rolling-pending'
    require(not pending.exists() and not pending.is_symlink(), 'An unfinished image-link update needs inspection')
    try:
        pending.symlink_to(image.name)
        os.replace(pending, DEST / 'coprint.img')
        sync_dir(DEST)
    finally:
        pending.unlink(missing_ok=True)


def cleanup_stage(rolling, stage, owner):
    compact_reports(stage, owner)
    # Never recursively remove a directory with surviving mounts.
    mounts = json.loads(rolling.b.run('findmnt', '-J', '-o', 'TARGET'))['filesystems']
    def points(items):
        for item in items:
            yield item['target']
            yield from points(item.get('children', []))
    busy = any(Path(p).is_relative_to(stage) for p in points(mounts))
    if busy:
        raise RuntimeError('Temporary mounts remain; keep ' + str(stage) + ' for inspection.')
    else:
        require(stage.parent == Path('/var/tmp') and stage.name.startswith('chromapad-rolling-')
                and not stage.is_symlink(), 'Unsafe temporary cleanup path')
        shutil.rmtree(stage)


def capacity_for_growth(used, free, source_growth):
    """Budget both saved-system and runtime growth; never shrink an image."""
    reserve = max(1024 ** 3, (used + 9) // 10)
    required = 2 * max(0, source_growth) + reserve
    quantum = 512 * 1024 ** 2
    return 0 if free >= required else ((required-free+quantum-1)//quantum)*quantum


def needs_content_check(record):
    return record is None or record.get('state') != 'READY' or record.get('needs_source_checksum', False)


def kernel_checkpoint(rolling):
    line = rolling.b.run('journalctl', '-k', '-n', '1', '-o', 'json', '--no-pager')
    require(bool(line), 'Kernel journal is unavailable; cannot monitor backup storage errors')
    return json.loads(line)['__CURSOR']


def check_kernel_storage(rolling, stage, cursor):
    output = rolling.b.run('journalctl', '-k', '--after-cursor', cursor, '-o', 'json', '--no-pager')
    pattern = r'I/O error|lost async page write|critical space allocation error|failed to convert unwritten extents|EXT4-fs (?:error|warning)|EXT4-fs.*(?:Aborting journal|Remounting filesystem read-only)'
    errors = []
    for line in output.splitlines():
        message = str(json.loads(line).get('MESSAGE', ''))
        if re.search(pattern, message, re.IGNORECASE):
            errors.append(message)
    rolling.b.record(stage/'reports/kernel-storage.json', {'status': 'FAIL' if errors else 'PASS', 'errors': errors})
    require(not errors, 'Kernel reported storage errors during backup: ' + '\n'.join(errors[:5]))


def ensure_capacity(rolling, stage, record, owner, source_used):
    c, b = rolling.c, rolling.b
    with c.usb_image(stage, image=IMAGE) as (_, root):
        usage = shutil.disk_usage(root)
    growth = source_used-record.get('source_used_bytes', source_used)
    extra = capacity_for_growth(usage.used, usage.free, growth)
    if not extra:
        return
    require(shutil.disk_usage(DEST).free > extra+512*1024**2, 'Insufficient NAS space for recovery growth')
    new_bytes = record['bytes']+extra
    # Validate the prospective geometry before making any changes.
    c.configure_layout(new_bytes)
    print('Growing recovery container by %.2f GiB' % (extra/1024**3), flush=True)
    import struct
    with IMAGE.open('r+b', buffering=0) as stream:
        prefix = bytearray(stream.read(512))
        require(struct.unpack_from('<II', prefix, 470) == (1050624, (record['bytes']-1050624*512)//512),
                'Unexpected recovery partition before growth')
        stream.truncate(new_bytes)
        struct.pack_into('<II', prefix, 470, *c.USB_PARTS[1])
        stream.seek(0)
        stream.write(prefix)
        os.fsync(stream.fileno())
    record['bytes'] = new_bytes
    save_state(record, owner)
    start, sectors = c.USB_PARTS[1]
    loop = b.run('losetup', '--find', '--show', '--offset', start*512, '--sizelimit', sectors*512, IMAGE)
    try:
        require(b.run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(IMAGE), 'Wrong growth loop')
        b.command('resize2fs', loop)
        b.command('e2fsck', '-f', '-n', loop)
    finally:
        b.cleanup_image(IMAGE, [loop], [])


def run(rolling):
    # The NAS automount is resolved by readiness() before this namespace fork.
    rolling.c.private()
    with Path('/run/lock/chromapad-rolling-backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, record = readiness(rolling)
        verify_contents = needs_content_check(record)
        cursor = kernel_checkpoint(rolling)
        before = plan['previous']
        source_used = shutil.disk_usage('/').used
        owner = Path(before['target']).stat()
        if record is None:
            with IMAGE.open('xb') as f:
                f.truncate(rolling.c.USB_BYTES)
                f.flush()
                os.fsync(f.fileno())
            os.chown(IMAGE, owner.st_uid, owner.st_gid)
            IMAGE.chmod(0o600)
            record = {'kind': 'chromapad-rolling-image', 'image': str(IMAGE),
                      'inode': IMAGE.stat().st_ino, 'bytes': rolling.c.USB_BYTES,
                      'initialized': False, 'geometry': rolling.metadata()}
        record.update(state='UPDATING', started_utc=stamp(), error=None,
                      needs_source_checksum=verify_contents)
        save_state(record, owner)
        stage = Path(tempfile.mkdtemp(prefix='chromapad-rolling-', dir='/var/tmp'))
        (stage / 'reports').mkdir()
        try:
            m = {**record['geometry'], 'stage': str(stage),
                 'verify_source_contents': verify_contents,
                 'build_id': 'debian-recovery-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}
            print('BACKUP_WORK=' + str(stage), flush=True)
            print('Updating one rolling Debian recovery image. Do not copy it until completion.', flush=True)
            if verify_contents:
                print('Full source content checksums required for initial capture or recovery after failure.', flush=True)
            try:
                with rolling.timed(stage, 'backup_work_total'):
                    if not record['initialized']:
                        with rolling.timed(stage, 'image_initialization'):
                            rolling.initialize(IMAGE, m)
                        record['initialized'] = True
                        save_state(record, owner)
                    else:
                        with rolling.timed(stage, 'outer_filesystem_checks'):
                            rolling.check_filesystems(IMAGE, stage, repair=True)
                        with rolling.timed(stage, 'capacity_check_and_growth'):
                            ensure_capacity(rolling, stage, record, owner, source_used)
                    with rolling.timed(stage, 'capture_total'):
                        rolling.capture(IMAGE, stage, m, printers.stopped_printers)
                    check_kernel_storage(rolling, stage, cursor)
                    rolling.b.phase(stage, 'Building and checking the recovery boot environment')
                    with rolling.timed(stage, 'finish_total'):
                        result = rolling.finish(IMAGE, stage, m)
                    check_kernel_storage(rolling, stage, cursor)
            finally:
                cleanup_stage(rolling, stage, owner)
            # In-place modification necessarily changes the previous target's
            # size/mtime identity. Require the link and inode to stay the same.
            now = anchor_identity(DEST)
            if before['target'] == str(IMAGE):
                expected = {**before, 'bytes': record['bytes']}
                require(all(now[k] == expected[k] for k in ('link', 'target', 'device', 'inode', 'bytes')),
                        'Current image identity changed during update')
                before = now
            activate(before, IMAGE)
            record.update(state='READY', completed_utc=stamp(), result=result, source_used_bytes=source_used,
                          needs_source_checksum=False)
            if verify_contents:
                record['last_source_checksum_utc'] = stamp()
                if isinstance(record.get('integrity_incident'), dict):
                    record['integrity_incident'].update(
                        next_backup_requires_content_checksums=False,
                        checksum_recaptured_utc=record['last_source_checksum_utc'],
                        resolved_by_build_id=result['build_id'])
            save_state(record, owner)
            print(json.dumps(result, indent=2), flush=True)
            print('BACKUP_COMPLETE_PRINTERS_RESTORED', flush=True)
        except BaseException as exc:
            record.update(state='FAILED', failed_utc=stamp(), error=str(exc), needs_source_checksum=True)
            save_state(record, owner)
            print('Backup failed. The rolling image is not ready to use: ' + str(exc), flush=True)
            raise



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ('plan', 'check', 'run'):
        mode.add_argument('--' + name, action='store_true')
    parser.add_argument('--private', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run with sudo')
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    os.umask(0o077)
    rolling = load_builder()
    plan, _ = readiness(rolling)
    if args.plan or args.check:
        print(json.dumps(plan, indent=2))
    else:
        run(rolling)


if __name__ == '__main__':
    main()

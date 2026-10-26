"""Maintain a sparse root filesystem inside one bootable recovery image.

Only regular image files are writable targets. The existing restore engine,
guarded boot and touchscreen remain shared with the earlier recovery builder.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import time
import subprocess
import uuid

import chromapad_debian_backup as b
import chromapad_recovery as r
import recovery_build_common as c


@contextlib.contextmanager
def timed(stage, name):
    """Record inclusive wall time, including failed phases, using a monotonic clock."""
    started_utc = b.stamp()
    start = time.monotonic()
    status = 'FAILED'
    print(f'TIMING_START {name}', flush=True)
    try:
        yield
        status = 'OK'
    finally:
        seconds = round(time.monotonic() - start, 3)
        print(f'TIMING_END {name} status={status} seconds={seconds:.3f}', flush=True)
        # Diagnostics must never mask a backup failure or prevent service cleanup.
        try:
            path = stage / 'reports/phase-timings.json'
            report = json.loads(path.read_text()) if path.exists() else {
                'schema': 1, 'duration_clock': 'monotonic',
                'note': 'Durations are inclusive; nested phases must not be added together.',
                'phases': []}
            report['phases'].append({'name': name, 'started_utc': started_utc,
                                     'seconds': seconds, 'status': status})
            pending = path.with_suffix('.pending')
            b.record(pending, report)
            os.replace(pending, path)
        except Exception as exc:
            print(f'TIMING_REPORT_ERROR {name}: {exc}', flush=True)


def module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), c.LOCAL / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def metadata():
    fat = uuid.uuid4().hex[:8].upper()
    return {'usb_root_uuid': str(uuid.uuid4()), 'usb_fat_uuid': fat[:4] + '-' + fat[4:],
            'usb_disk_id': uuid.uuid4().hex[:8], 'root_uuid': b.ROOT_UUID,
            'fat_uuid': b.FAT_UUID, 'cid': b.CID}


def require_regular(path, size=None):
    info = path.lstat()
    b.require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), 'Not a regular image file: ' + str(path))
    if size is not None:
        b.require(info.st_size == size, 'Unexpected image length: ' + str(path))
    return info


def initialize(image, m):
    """Format only the two verified partitions of the owned USB image file."""
    require_regular(image, c.USB_BYTES)
    b.require(not b.run('losetup', '-j', image), 'Recovery image already has loop devices')
    prefix = bytearray((c.LOCAL / 'assets/usb-prefix.bin').read_bytes())
    b.require(len(prefix) == 1048576 and prefix[510:512] == b'\x55\xaa', 'Invalid USB boot prefix')
    struct.pack_into('<I', prefix, 440, int(m['usb_disk_id'], 16))
    for i, geometry in enumerate(c.USB_PARTS):
        b.require(struct.unpack_from('<II', prefix, 446 + i * 16 + 8) == tuple(geometry), 'USB prefix layout differs')
    with image.open('r+b', buffering=0) as f:
        f.write(prefix)
        os.fsync(f.fileno())
    for i, (start, sectors) in enumerate(c.USB_PARTS):
        loop = b.run('losetup', '--find', '--show', '--offset', start * 512,
                     '--sizelimit', sectors * 512, image)
        try:
            b.require(b.run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong format loop')
            if i == 0:
                b.command('mkfs.vfat', '-F', '32', '-i', m['usb_fat_uuid'].replace('-', ''), '-n', 'CP_RECOVERY', loop)
            else:
                b.command('mkfs.ext4', '-F', '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0',
                          '-U', m['usb_root_uuid'], '-L', 'CP_RECOVERY', '-m', '1', loop)
        finally:
            b.cleanup_image(image, [loop], [])


def check_filesystems(image, stage, repair=False):
    """Conservative journal recovery is allowed only on the owned image."""
    require_regular(image, c.USB_BYTES)
    b.require(not b.run('losetup', '-j', image), 'Recovery image is in use')
    for i, (start, sectors) in enumerate(c.USB_PARTS):
        args = ['losetup', '--find', '--show', '--offset', start * 512, '--sizelimit', sectors * 512]
        if not repair:
            args.append('--read-only')
        loop = b.run(*args, image)
        try:
            b.require(b.run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong filesystem-check loop')
            cmd = ['fsck.vfat', '-a' if repair else '-n'] if i == 0 else ['e2fsck', '-f', '-p' if repair else '-n']
            with (stage / 'reports' / ('outer-fat-fsck.txt' if i == 0 else 'outer-root-fsck.txt')).open('w') as log:
                result = subprocess.run([*cmd, loop], stdout=log, stderr=subprocess.STDOUT)
            b.require(result.returncode in ((0, 1) if repair else (0,)),
                      'Recovery filesystem needs inspection; see ' + str(stage / 'reports'))
        finally:
            b.cleanup_image(image, [loop], [])


@contextlib.contextmanager
def mounted_payload(payload, stage):
    require_regular(payload, r.RAW_BYTES)
    b.require(not b.run('losetup', '-j', payload), 'Saved root filesystem already mounted')
    with timed(stage, 'payload_fsck_before'), (stage / 'reports/root-fsck-before.txt').open('w') as log:
        result = subprocess.run(['e2fsck', '-f', '-p', str(payload)], stdout=log, stderr=subprocess.STDOUT)
    b.require(result.returncode in (0, 1), 'Saved root filesystem needs manual inspection')
    b.require(b.run('blkid', '-p', '-c', '/dev/null', '-s', 'UUID', '-o', 'value', payload) == b.ROOT_UUID,
              'Saved root filesystem UUID differs')
    point = stage / 'saved-root'
    point.mkdir(exist_ok=True)
    loops, mounts = [], []
    try:
        loop = b.run('losetup', '--find', '--show', payload)
        loops.append(loop)
        b.require(b.run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(payload), 'Wrong saved-root loop')
        b.command('mount', '-t', 'ext4', '-o', 'rw,noatime,nosuid,nodev', loop, point)
        mounts.append((loop, point))
        yield point
    finally:
        b.cleanup_image(payload, loops, mounts)


def clear_child(parent, name):
    """Remove one explicitly named generated child, never a mounted directory."""
    b.require(Path(name).name == name and name not in ('.', '..'), 'Unsafe generated filename')
    path = parent / name
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        b.require(not path.is_mount(), 'Generated directory is mounted')
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def disarm(fat):
    for name in ('recovery-arm.json', 'recovery-release.json', 'AUTOMATIC-RESTORE.txt'):
        clear_child(fat, name)
    b.command('sync', '-f', fat)


def hash_payload(path):
    value = hashlib.sha256()
    done, next_report = 0, 1024 ** 3
    with path.open('rb', buffering=0) as stream:
        while block := stream.read(4 * 1024 ** 2):
            if done == 0:
                r.check_superblock(block)
            value.update(block)
            done += len(block)
            if done >= next_report:
                print(f'Checking saved root: {done // 1024 ** 2} / {r.RAW_BYTES // 1024 ** 2} MiB', flush=True)
                next_report += 1024 ** 3
    b.require(done == r.RAW_BYTES, 'Wrong saved root length')
    return value.hexdigest()


def runtime_copy(source, root, stage):
    # Keep the payload in place while refreshing the smaller recovery runtime.
    # The complete /home and /root contents remain in the saved root filesystem.
    rules = ['--include=/home/cp/', '--include=/home/cp/.ssh/***',
             '--include=/home/cp/.bashrc', '--include=/home/cp/.profile',
             '--include=/home/cp/.bash_logout', '--exclude=/home/cp/*',
             '--exclude=/root/*', '--exclude=/opt/chromapad-recovery/***',
             '--exclude=/var/lib/chromapad-recovery/***']
    with (stage / 'reports/runtime-copy.txt').open('w') as log:
        b.command('rsync', '-aHAXx', '--numeric-ids', '--delete', *rules,
                  str(source) + '/', str(root) + '/', stdout=log, stderr=subprocess.STDOUT)
    # This directory is generated recovery state, not the captured user's data.
    state = root / 'var/lib/chromapad-recovery'
    b.require(not state.is_symlink(), 'Symlinked recovery state')
    state.mkdir(parents=True, exist_ok=True)
    for name in list(state.iterdir()):
        clear_child(state, name.name)


def copy_system(source, destination, log_path, allow_vanished=False):
    """Use rsync's normal size/mtime quick check, including on the final pass."""
    with log_path.open('w') as log:
        result = subprocess.run([*b.RSYNC, '--stats', str(source).rstrip('/') + '/',
                                 str(destination) + '/'], stdout=log, stderr=subprocess.STDOUT)
    b.require(result.returncode in ((0, 24) if allow_vanished else (0,)), 'System file copy failed')


def capture(image, stage, m, printer_context):
    """Update the embedded filesystem; never create another NAS image."""
    inputs = stage / 'inputs'
    inputs.mkdir()
    (stage / 'payload').mkdir()
    reference = json.loads((c.LOCAL / 'protected-reference.json').read_text())
    with c.usb_image(stage, True, image=image) as (fat, root):
        disarm(fat)
        bundle = root / 'opt/chromapad-recovery'
        b.require(not bundle.is_symlink(), 'Symlinked payload directory')
        bundle.mkdir(parents=True, exist_ok=True)
        payload = bundle / 'root.ext4'
        if not payload.exists():
            with payload.open('xb') as f:
                f.truncate(r.RAW_BYTES)
            b.command('mkfs.ext4', '-F', '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0',
                      '-U', b.ROOT_UUID, '-L', 'CP_DEBIAN_EMMC', '-m', '1', payload)
        require_regular(payload, r.RAW_BYTES)
        with mounted_payload(payload, stage) as saved:
            with timed(stage, 'printer_stop_capture_and_restart'), printer_context():
                identity = b.source_identity()
                with timed(stage, 'protected_firmware_checks'):
                    for start, end in b.RANGES:
                        b.require(b.digest(identity['disk'], start, end - start) == reference[f'{start}:{end}'],
                                  'Protected firmware reference differs')
                    for suffix in ('boot0', 'boot1'):
                        b.require(b.digest(identity['disk'] + suffix) == reference[suffix], 'Hardware boot reference differs')
                b.phase(stage, 'Updating the saved system inside the recovery image')
                with timed(stage, 'initial_rsync'):
                    copy_system(Path('/'), saved, stage / 'reports/rsync-initial.txt', allow_vanished=True)
                with timed(stage, 'quiesced_final_copy_and_comparison'):
                    with b.quiesced(stage):
                        b.phase(stage, 'Final consistent copy; application writers are paused')
                        copy_system(Path('/'), saved, stage / 'reports/rsync-final.txt')
                        bootfat = inputs / 'fat'
                        bootfat.mkdir()
                        b.command('rsync', '-rt', '--delete', '--modify-window=1', '/boot/emmc/', str(bootfat) + '/')
                        comparison = b.comparison_result(b.run(*b.RSYNC, '--dry-run', '--itemize-changes', '/', str(saved) + '/'))
                        b.record(stage / 'reports/root-comparison.json', comparison)
                        b.require(not comparison['blocking'], 'Source changed during the final copy')
                        b.require(not b.run('rsync', '-rcn', '--delete', '--itemize-changes', '/boot/emmc/', str(bootfat) + '/'), 'Boot copy differs')
                        boot = b.validate_boot(bootfat, saved)
                        b.require(b.source_identity() == identity, 'Source boot changed during backup')
                        shutil.copytree(saved / 'usr/lib/chromapad/emmc', inputs / 'helpers')
                        m['kernel'] = json.loads((saved / 'usr/lib/chromapad/emmc/identifiers.json').read_text())['kernel']
                        shutil.copytree(bootfat / 'debian' / boot['generation'], inputs / 'emmc-boot')
                        shutil.copyfile(saved / 'boot' / ('initrd.img-' + m['kernel']), inputs / 'initrd.raw')
                        b.require((saved / 'usr/lib/python3/dist-packages/PIL/Image.py').is_file(), 'Recovery display library missing')
                        policy = saved / 'etc/tmpfiles.d/chromapad-nginx-logs.conf'
                        b.require(policy.is_file() and 'd /var/log/nginx 0755 root adm -' in policy.read_text(), 'nginx log recovery policy missing')
                        b.command('sync', '-f', saved)
                b.record(stage / 'reports/source.json', identity)
            # Klipper and application services are restored before offline work.
            b.phase(stage, 'Reclaiming unused blocks in the saved filesystem')
            with timed(stage, 'payload_trim'):
                b.command('fstrim', '--verbose', saved)
            b.command('mount', '-o', 'remount,ro', saved)
            b.phase(stage, 'Refreshing the recovery environment')
            with timed(stage, 'runtime_refresh'):
                runtime_copy(saved, root, stage)
        with timed(stage, 'payload_fsck_after'):
            with (stage / 'reports/root-fsck-after.txt').open('w') as log:
                b.command('e2fsck', '-f', '-n', payload, stdout=log, stderr=subprocess.STDOUT)
        b.phase(stage, 'Verifying the complete saved root filesystem')
        with timed(stage, 'payload_sha256'):
            checksum = hash_payload(payload)
        m['backup_sha256'] = checksum
        b.record(stage / 'reports/root-payload.json', {'encoding': 'raw', 'file': 'root.ext4',
                 'bytes': r.RAW_BYTES, 'sha256': checksum, 'raw_bytes': r.RAW_BYTES, 'raw_sha256': checksum})
        # Only generated bundle metadata is replaced; the raw filesystem stays.
        for child in list(bundle.iterdir()):
            if child.name != 'root.ext4':
                clear_child(bundle, child.name)
        b.require(shutil.disk_usage(root).free > 768 * 1024 ** 2, 'Recovery image needs 768 MiB free for boot backup and metadata')
        b.command('sync', '-f', root)
    for name in ('usb-boot.cmd', 'usb-prefix.bin'):
        shutil.copyfile(c.LOCAL / 'assets' / name, inputs / name)
    guard = (c.LOCAL / 'probe-emmc-guard.sh').read_text().replace('38327d56-529b-498f-8b1a-e81b534bfeb9', m['usb_root_uuid'])
    needle = 'probe_cid=$(cat "$probe_device/cid")'
    b.require(guard.count(needle) == 1, 'Unexpected recovery guard template')
    guard = guard.replace(needle, needle + '\n[ "$probe_cid" = ' + b.CID + ' ] || fail "Target CID mismatch"')
    (stage / 'recovery-emmc-guard.sh').write_text(guard)
    b.command('sh', '-n', stage / 'recovery-emmc-guard.sh')
    b.record(stage / 'reports/base-built.json', {'status': 'PASS', 'rolling': True})
    b.record(stage / 'reports/inputs.json', {'status': 'PASS', 'source_root_sha256': checksum})


def finish(image, stage, m):
    with timed(stage, 'recovery_configuration_and_packaging'):
        module('build-recovery-config.py').configure(stage, m, image=image, rolling=True)
    with timed(stage, 'layout_audit'):
        module('audit-recovery-layout.py').audit(stage, m, image=image)
    with timed(stage, 'image_validation'):
        module('validate-recovery-image.py').validate(stage, m, image=image)
    with timed(stage, 'launcher_smoke'):
        module('smoke-recovery-launcher.py').smoke(stage, m, image=image)
    b.require(not b.run('losetup', '-j', image), 'Recovery image still mounted')
    manifest = json.loads((stage / 'payload/manifest.json').read_text())
    return {'status': 'VERIFIED_ROLLING_RECOVERY_IMAGE', 'image': str(image),
            'build_id': m['build_id'], 'bytes': image.stat().st_size,
            'manifest_sha256': b.digest(stage / 'payload/manifest.json'),
            'root_sha256': manifest['root_payload']['raw_sha256'],
            'automatic_restore': True, 'physical_restore_performed': False}

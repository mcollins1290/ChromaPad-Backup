#!/usr/bin/python3
"""Build with the verified Debian engine, then atomically publish coprint.img."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import uuid
import printer_lifecycle as printers

BUILDER = Path('/usr/local/lib/chromapad-recovery-builder')
PROJECT = Path('/media/raid1/coprint_2_debian')
DEST = Path('/media/raid1/coprint')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(4 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, data):
    pending = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with pending.open('x') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(pending, path)
        sync_dir(path.parent)
    finally:
        pending.unlink(missing_ok=True)


def anchor_identity(dest):
    anchor = dest / 'coprint.img'
    require(dest.is_dir() and not dest.is_symlink(), 'Invalid coprint directory')
    require(anchor.exists(), 'Existing coprint.img is missing or dangling')
    target = anchor.resolve(strict=True)
    require(target.parent == dest.resolve(), 'Existing image escapes coprint directory')
    st = target.stat()
    require(stat.S_ISREG(st.st_mode), 'Existing image is not a regular file')
    return {'link': os.readlink(anchor) if anchor.is_symlink() else None,
            'target': str(target), 'device': st.st_dev, 'inode': st.st_ino,
            'bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def verify_release(image, output):
    require(image.is_absolute() and not image.is_symlink(), 'Image must be a published regular file')
    require(image.resolve() == image, 'Image path contains symlinks')
    require(image.parent.parent == output and image.name == 'chromapad-debian-recovery-auto.img',
            'Image outside release directory')
    release = json.loads((image.parent / 'release.json').read_text())
    build = release['build_id']
    require(re.fullmatch(r'debian-recovery-\d{8}T\d{6}Z', build) is not None
            and image.parent.name == build, 'Invalid release identity')
    require(release['status'] == 'VERIFIED_AUTOMATIC_RECOVERY_IMAGE'
            and release['automatic_restore'] is True, 'Release did not pass validation')
    require(release['image'] == str(image), 'Release names another image')
    require(stat.S_ISREG(image.stat().st_mode) and image.stat().st_size == release['bytes'],
            'Image size mismatch')
    require(digest(image.parent / 'manifest.json') == release['manifest_sha256'], 'Manifest mismatch')
    for name in ('base-built', 'config-built', 'initramfs-report', 'image-validation',
                 'launcher-smoke', 'usb-layout', 'inputs'):
        gate = json.loads((image.parent / 'validation' / (name + '.json')).read_text())
        require(gate.get('status') == 'PASS', 'Failed release gate: ' + name)
        require('manifest_sha256' not in gate or gate['manifest_sha256'] == release['manifest_sha256'],
                'Release gate pin mismatch: ' + name)
    require(digest(image) == release['sha256'], 'Image checksum mismatch')
    return release


def publish(image, output, dest, before):
    """Retain prior data and switch only the stable name after all verification."""
    release = verify_release(image, output)
    require(anchor_identity(dest) == before, 'coprint.img changed during backup; refusing to replace it')
    name = 'coprint_' + release['build_id'] + '.img'
    dated = dest / name
    require(not os.path.lexists(dated), 'Dated image already exists')
    # A same-filesystem hardlink avoids another 13GB copy and exposes the exact
    # verified bytes. Neither published image may subsequently be edited in place.
    require(image.stat().st_dev == dest.stat().st_dev, 'Release and destination must share the NAS filesystem')
    os.link(image, dated)
    require(os.path.samefile(image, dated), 'Published image is not the verified inode')
    previous = before['target']
    if before['link'] is None:
        preserved = dest / ('coprint-preserved-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.img')
        os.link(dest / 'coprint.img', preserved)
        previous = str(preserved)
    record = {'status': 'COPRINT_IMAGE_VERIFIED', 'utc': stamp(), 'image': str(dated),
              'sha256': release['sha256'], 'bytes': release['bytes'],
              'manifest_sha256': release['manifest_sha256'], 'build_id': release['build_id'],
              'previous_image_preserved': previous, 'source_release': str(image.parent),
              'automatic_restore': True}
    meta = dated.with_suffix('.json')
    atomic_json(meta, record)
    os.chown(meta, image.stat().st_uid, image.stat().st_gid)
    meta.chmod(0o600)
    sync_dir(dest)
    require(anchor_identity(dest) == before, 'coprint.img changed before publication')
    pending = dest / ('.coprint-pending-' + uuid.uuid4().hex)
    try:
        pending.symlink_to(name)
        os.replace(pending, dest / 'coprint.img')
        sync_dir(dest)
    finally:
        pending.unlink(missing_ok=True)
    return {**record, 'status': 'COPRINT_IMAGE_PUBLISHED', 'anchor': str(dest / 'coprint.img')}


def completed_release(text):
    decoder = json.JSONDecoder()
    results = []
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('status') == 'VERIFIED_AUTOMATIC_RECOVERY_IMAGE':
            results.append(value)
    require(len(results) == 1, 'Builder did not return one verified release result')
    return results[0]


def run_builder():
    tail = ''
    with subprocess.Popen([sys.executable, str(BUILDER / 'chromapad_recovery_backup.py'), '--capture'],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True) as child:
        try:
            for line in child.stdout:
                print(line, end='', flush=True)
                tail = (tail + line)[-65536:]
            require(child.wait() == 0, 'Debian backup/build failed; coprint.img was not changed')
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=170)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
            raise
    return completed_release(tail)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--run', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run with sudo')
    sys.path.insert(0, str(BUILDER.resolve(strict=True)))
    import chromapad_debian_backup as backup
    import chromapad_recovery_backup as workflow
    workflow.verify_tools(BUILDER.resolve())
    backup.check_destination(backup.BASE)
    require(DEST.resolve() == DEST, 'Symlinked coprint destination')
    before = anchor_identity(DEST)
    checked = printers.plan()
    if args.plan or args.check:
        print(json.dumps({'status': 'PLAN_ONLY', 'destination': str(DEST / 'coprint.img'),
                          'previous': before, **checked,
                          'writes_performed': False}, indent=2))
        return
    def interrupted(signum, frame):
        raise RuntimeError('Backup interrupted by signal ' + str(signum))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    os.umask(0o077)
    with (PROJECT / '.coprint-promotion.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with printers.stopped_printers():
            backup.source_identity()
            latest = PROJECT / 'output/debian-recovery-latest.img'
            old_release = latest.resolve() if latest.exists() else None
            print(stamp() + ' Creating and verifying a fresh Debian backup/recovery image.', flush=True)
            # The tested builder retains its own exclusive lock and consistency checks.
            built = run_builder()
            require(latest.is_symlink(), 'Builder did not publish its latest image link')
            image = latest.resolve(strict=True)
            require(image != old_release and str(image) == built['image'], 'Latest image is not the release from this run')
            print(stamp() + ' Verifying the completed image before updating coprint.img.', flush=True)
            result = publish(image, PROJECT / 'output', DEST, before)
            print(json.dumps(result, indent=2), flush=True)
        print('BACKUP_COMPLETE_PRINTERS_RESTORED', flush=True)


if __name__ == '__main__':
    main()

#!/usr/bin/python3
"""One-time expansion of the owned legacy rolling image; leaves it FAILED until recaptured."""
import fcntl
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import rolling_backup as controller


def main():
    rolling = controller.load_builder()
    b, c = rolling.b, rolling.c
    controller.require(os.geteuid() == 0, 'Root required')
    # Resolve the NAS before entering the private mount namespace.
    controller.require(controller.DEST.is_dir(), 'NAS unavailable')
    c.private()
    with Path('/run/lock/chromapad-rolling-backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active = b.run('systemctl', 'show', 'chromapad-backup.service', '-p', 'ActiveState', '--value')
        controller.require(active in ('inactive', 'failed'), 'Backup service is running')
        controller.verify_nas_mount(json.loads(b.run('findmnt', '-J', '-T', controller.DEST))['filesystems'])
        old_size = sum(c.LEGACY_USB_PARTS[1]) * 512
        image = controller.IMAGE
        record = controller.managed_record(image, controller.STATE, old_size)
        controller.require(record is not None and record['initialized'], 'Expected initialized legacy image')
        controller.require(not b.run('losetup', '-j', image), 'Image is in use')
        controller.require(__import__('shutil').disk_usage(controller.DEST).free >= c.USB_BYTES-old_size+512*1024**2,
                           'Insufficient NAS growth space')
        owner = image.stat()
        with image.open('rb') as f:
            prefix = bytearray(f.read(512))
        controller.require(prefix[510:] == b'\x55\xaa', 'Invalid MBR')
        for i, geometry in enumerate(c.LEGACY_USB_PARTS):
            controller.require(struct.unpack_from('<II', prefix, 454+i*16) == tuple(geometry), 'Unexpected old geometry')
        controller.require(prefix[478:510] == bytes(32), 'Unexpected extra partitions')
        controller.require(format(struct.unpack_from('<I', prefix, 440)[0], '08x') == record['geometry']['usb_disk_id'],
                           'Disk ID mismatch')
        record.update(state='FAILED', error='Image expansion in progress; successful backup required')
        controller.save_state(record, owner)
        # Retain the original partition table for diagnosis. Never shrink on failure.
        stage = Path(tempfile.mkdtemp(prefix='chromapad-image-expansion-', dir='/var/tmp'))
        (stage/'original-mbr.bin').write_bytes(prefix)
        print('Expansion diagnostics: ' + str(stage), flush=True)
        with image.open('r+b', buffering=0) as f:
            f.truncate(c.USB_BYTES)
            struct.pack_into('<II', prefix, 470, *c.USB_PARTS[1])
            f.seek(0)
            f.write(prefix)
            os.fsync(f.fileno())
        record.update(bytes=c.USB_BYTES, error='Expanded image requires filesystem repair and recapture')
        controller.save_state(record, owner)
        start, sectors = c.USB_PARTS[1]
        loop = b.run('losetup', '--find', '--show', '--offset', start*512, '--sizelimit', sectors*512, image)
        try:
            controller.require(b.run('losetup', '-n', '-O', 'BACK-FILE', loop) == str(image), 'Wrong expansion loop')
            controller.require(b.run('blkid', '-p', '-c', '/dev/null', '-s', 'UUID', '-o', 'value', loop) ==
                               record['geometry']['usb_root_uuid'], 'Wrong outer filesystem UUID')
            with (stage/'outer-fsck.txt').open('w') as log:
                result = subprocess.run(['e2fsck', '-f', '-p', loop], stdout=log, stderr=subprocess.STDOUT)
            controller.require(result.returncode in (0, 1), 'Filesystem needs manual inspection: '+str(stage))
            b.command('resize2fs', loop)
        finally:
            b.cleanup_image(image, [loop], [])
        print('Expanded recovery image to %.2f GiB. Successful backup still required.' % (c.USB_BYTES/1024**3), flush=True)


if __name__ == '__main__':
    main()

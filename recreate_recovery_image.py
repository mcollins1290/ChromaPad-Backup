#!/usr/bin/python3
"""Recreate the last verified snapshot in a smaller, freshly formatted container."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import rolling_backup as controller


def remote(rolling, request):
    helper = (rolling.c.LOCAL / 'nas_payload_hash.py').read_text()
    repacker = (rolling.c.LOCAL / 'nas_repack_image.py').read_text()
    script = ('import sys,types\nmodule=types.ModuleType("nas_payload_hash")\n'
              'sys.modules["nas_payload_hash"]=module\nexec(' + repr(helper) + ',module.__dict__)\n'
              'REQUEST=' + repr(request) + '\n' + repacker)
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=3', 'root@192.168.0.2',
               'unshare', '--mount', '--propagation', 'private', 'python3', '-']
    with tempfile.NamedTemporaryFile(mode='w+', prefix='chromapad-repack-', suffix='.log', delete=False) as log:
        print('NAS recreation log: ' + log.name, flush=True)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True)
        process.communicate(script, timeout=7200)
        log.seek(0)
        output = log.read()
    controller.require(process.returncode == 0, 'NAS recreation failed; inspect ' + log.name + '\n' + output[-2000:])
    records = [json.loads(line.removeprefix('REPACK_RESULT ')) for line in output.splitlines()
               if line.startswith('REPACK_RESULT ')]
    controller.require(len(records) == 1, 'Missing or ambiguous recreation result')
    return records[0]


def publish(candidate, record, result, owner):
    image = controller.IMAGE
    previous = image.with_name('.coprint-debian.previous.img')
    journal = image.with_name('.coprint-debian-recreation.json')
    controller.require(not previous.exists() and not previous.is_symlink()
                       and not journal.exists() and not journal.is_symlink(), 'Recreation transaction already exists')
    info = candidate.lstat()
    controller.require(info.st_ino == result['candidate_inode'] and info.st_size == result['image_bytes']
                       and info.st_mtime_ns == result['candidate_mtime_ns']
                       and not candidate.is_symlink() and info.st_nlink == 1, 'Recreated candidate identity differs')
    controller.managed_record(image, controller.STATE, record['bytes'])
    updated = copy.deepcopy(record)
    updated.update(inode=info.st_ino, bytes=info.st_size, recreated_utc=controller.stamp(),
                   source_used_bytes=shutil.disk_usage('/').used,
                   recreation={'previous_bytes': record['bytes'], **result})
    updated['result']['bytes'] = info.st_size
    controller.atomic_json(journal, {'previous_record': record, 'replacement_record': updated,
                                     'previous_image': str(previous), 'candidate': str(candidate)})
    os.link(image, previous)
    try:
        os.replace(candidate, image)
        controller.save_state(updated, owner)
        controller.sync_dir(image.parent)
    except BaseException:
        # Keep the old verified image usable if publication fails.
        if image.exists() and image.stat().st_ino == record['inode']:
            previous.unlink()
        else:
            if image.exists():
                os.replace(image, candidate)
            os.replace(previous, image)
        controller.save_state(record, owner)
        raise
    controller.managed_record(image, controller.STATE, info.st_size)
    previous.unlink()
    journal.unlink()
    controller.sync_dir(image.parent)
    return updated


def validate_candidate(rolling, candidate, record, result):
    """Run the existing image, ARM toolchain and launcher validators again."""
    stage = Path(tempfile.mkdtemp(prefix='chromapad-recreation-validation-', dir='/var/tmp'))
    print('Recreation validation reports: ' + str(stage), flush=True)
    (stage/'reports').mkdir()
    (stage/'payload').mkdir()
    (stage/'inputs').mkdir()
    rolling.c.configure_layout(result['image_bytes'])
    m = {**record['geometry'], 'build_id': record['result']['build_id'],
         'backup_sha256': record['result']['root_sha256']}
    with rolling.c.usb_image(stage, image=candidate) as (_, root):
        shutil.copy2(root/'opt/chromapad-recovery/release.json', stage/'payload/release.json')
        shutil.copytree(root/'usr/lib/chromapad/emmc', stage/'inputs/helpers')
    (stage/'reports/config-built.json').write_text(json.dumps({'status':'PASS','existing_verified_snapshot':True}))
    rolling.module('audit-recovery-layout.py').audit(stage, m, image=candidate)
    rolling.module('validate-recovery-image.py').validate(stage, m, image=candidate)
    rolling.module('smoke-recovery-launcher.py').smoke(stage, m, image=candidate)
    controller.require(not rolling.b.run('losetup', '-j', candidate), 'Candidate still in use on pad')
    # Read-only validation may update access time, but must not change image bytes.
    controller.require(candidate.stat().st_mtime_ns == result['candidate_mtime_ns'], 'Candidate changed in validation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Build, verify and replace; otherwise report sizing only')
    parser.add_argument('--private', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    rolling = controller.load_builder()
    controller.require(os.geteuid() == 0, 'Root required')
    if args.run:
        rolling.c.private()
    with Path('/run/lock/chromapad-rolling-backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, record = controller.readiness(rolling)
        controller.require(record and record['state'] == 'READY' and record['initialized'],
                           'Recreation requires the last verified READY image')
        controller.require(plan['previous']['target'] == str(controller.IMAGE), 'Current anchor selects another image')
        owner = controller.IMAGE.stat()
        start, sectors = rolling.c.USB_PARTS[1]
        request = {'operation': 'plan', 'image_bytes': record['bytes'], 'image_inode': record['inode'],
                   'start_sector': start, 'sectors': sectors, **record['geometry'],
                   'raw_bytes': rolling.r.RAW_BYTES,
                   'manifest_sha256': record['result']['manifest_sha256'],
                   'root_sha256': record['result']['root_sha256']}
        sizing = remote(rolling, request)
        print(json.dumps({'current_bytes': record['bytes'], 'recreated': sizing,
                          'writes_performed': False}, indent=2), flush=True)
        if not args.run:
            return
        controller.require(sizing['image_bytes'] < record['bytes'], 'Recreation would not make this image smaller')
        request.update(operation='repack', new_image_bytes=sizing['image_bytes'])
        result = remote(rolling, request)
        controller.require(result.get('cleanup_complete') is True and result.get('copied_contents_verified') is True
                           and result['manifest_sha256'] == request['manifest_sha256']
                           and result['root_sha256'] == request['root_sha256']
                           and result['image_bytes'] == sizing['image_bytes'], 'Recreation verification report differs')
        controller.require(controller.anchor_identity(controller.DEST) == plan['previous'],
                           'Source image or anchor changed during recreation')
        candidate = controller.IMAGE.with_name('.coprint-debian.rebuild.img')
        validate_candidate(rolling, candidate, record, result)
        publish(candidate, record, result, owner)
        print('RECREATION_COMPLETE: READY snapshot preserved; %.2f GiB -> %.2f GiB' %
              (record['bytes']/1024**3, result['image_bytes']/1024**3), flush=True)


if __name__ == '__main__':
    main()

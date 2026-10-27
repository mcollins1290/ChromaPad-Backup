"""Build or reuse the guarded initramfs with content-pinned target tools."""
import json
import re
import shutil
import time
import recovery_build_common as c
from recovery_artifacts import file_record, root_records


def guarded_initramfs(root, stage, guard, cache):
    started = time.monotonic()
    tools = ['/usr/sbin/blockdev', '/usr/sbin/blkid', '/usr/bin/sha256sum']
    # Resolve the actual chroot loader choices, not a package-version shortcut.
    with c.emulator(), c.minimal_devices(root):
        libraries = c.arm(root, '/usr/bin/ldd', *tools)
    paths = tools + re.findall(r'(?<!\S)(/[^\s():]+)', libraries)
    paths += ['/usr/bin/ldd', '/usr/share/initramfs-tools/hook-functions',
              '/var/lib/dpkg/status', '/etc/ld.so.cache']
    inputs = {'schema': 1, 'implementation': file_record(__file__),
              'builder': file_record(c.LOCAL / 'build_recovery_initramfs_arm.py'),
              'mkimage': file_record(shutil.which('mkimage')),
              'initrd': file_record(stage / 'inputs/initrd.raw'), 'guard': file_record(guard),
              'tools': root_records(root, paths)}
    target = stage / 'guarded-uInitrd'
    def build():
        with c.scratch(root) as work:
            for source, name in [(stage/'inputs/initrd.raw', 'initrd.raw'), (guard, 'recovery-emmc-guard.sh'),
                                 (c.LOCAL/'build_recovery_initramfs_arm.py', 'build.py')]:
                shutil.copyfile(source, work/name)
            with c.emulator(), c.minimal_devices(root):
                result = c.arm(root, '/usr/bin/python3', '/var/tmp/recovery-build/build.py')
                (stage/'reports/initramfs-build.txt').write_text(result+'\n')
            report = json.loads((work/'initramfs-report.json').read_text())
            c.b.require(report['status'] == 'PASS', 'Initramfs inventory verification failed')
            c.b.command('mkimage', '-A', 'arm64', '-O', 'linux', '-T', 'ramdisk', '-C', 'gzip',
                        '-a', '0', '-e', '0', '-n', 'ChromaPad protected recovery',
                        '-d', work/'guarded-initrd.raw', target)
        return report
    report = cache.get('initramfs', inputs, target, build, started=started)
    c.b.record(stage/'reports/initramfs-report.json', report)
    if not (stage/'reports/initramfs-build.txt').exists():
        (stage/'reports/initramfs-build.txt').write_text('Reused verified guarded initramfs; input fingerprints match.\n')
    return target

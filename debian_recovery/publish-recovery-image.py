"""Seal a new automatic-recovery image; never edit an older published image."""
import json, os, shutil
import recovery_build_common as c
from recovery_build_common import b
c.private()
with c.project() as (stage,m):
    pin=json.loads((stage/'payload/release.json').read_text())['manifest_sha256']
    for name in ('base-built','config-built','initramfs-report','image-validation','launcher-smoke','usb-layout','inputs'):
        value=json.loads((stage/'reports'/(name+'.json')).read_text())
        b.require(value['status']=='PASS','Failed release gate: '+name)
        if 'manifest_sha256' in value:b.require(value['manifest_sha256']==pin,'Gate used different manifest')
    tests=json.loads((c.LOCAL/'recovery-guard-tests.json').read_text())
    b.require(len(tests['cases'])==21 and all(x['result']=='PASS' for x in tests['cases']),'Guard tests incomplete')
    b.require(not b.run('losetup','-j',stage/'recovery-usb.img'),'Image still mounted')
    output=c.BASE/'output'/m['build_id'];b.require(not output.exists(),'Release already exists')
    output.mkdir(mode=0o700)
    image=output/'chromapad-debian-recovery-auto.img'
    # The new release stays unreferenced until its checksum and evidence exist.
    os.rename(stage/'recovery-usb.img',image)
    checksum=b.digest(image)
    result={'status':'VERIFIED_AUTOMATIC_RECOVERY_IMAGE','build_id':m['build_id'],'image':str(image),'bytes':image.stat().st_size,'sha256':checksum,'manifest_sha256':pin,'backup_image':m['backup_image'],'backup_image_sha256':m['backup_sha256'],'automatic_restore':True,'on_screen_progress':True,'physical_media_written':False,'physical_restore_performed':False,'physical_boot_of_this_image_verified':False}
    b.record(output/'release.json',result)
    (output/'SHA256SUMS').write_text(checksum+'  '+image.name+'\n')
    shutil.copyfile(stage/'payload/manifest.json',output/'manifest.json')
    shutil.copytree(stage/'reports',output/'validation')
    shutil.copytree(c.LOCAL,output/'build-sources',ignore=shutil.ignore_patterns('__pycache__'))
    (output/'README.txt').write_text('''ChromaPad Debian automatic recovery USB

Write chromapad-debian-recovery-auto.img to the SanDisk with your image writer.
This is the USB-ready image. debian-emmc.img in backups is a source archive.

WARNING: Booting this USB automatically replaces the pinned pad's Debian root
with this backup. It validates the exact pad, layout, backup and protected
firmware first. It preserves the partition table and hardware boot areas.
Do not boot it merely to browse the backup.

The large recovery screen reports six stages, with byte-based progress during
backup checking, root copying and complete eMMC readback. The final green screen
appears only after filesystem, boot and firmware verification and cleanup pass.
Keep power and USB connected throughout. On completion, tap the large Shut down
button. It is enabled only after verified recovery and cleanup have finished.
After the pad has fully shut down,
remove USB, and cold-start the pad. There is no automatic poweroff or reboot.
A red failure screen means leave the pad on and inspect the saved logs.

Logs: journalctl -b -u chromapad-recovery
Durable results: /var/lib/chromapad-recovery/BUILD-ID on the recovery USB.
Progress: /run/chromapad-recovery-progress.json (RAM, updated twice per second).
Any attempted restore blocks automatic repetition on this written USB, even
after interruption. Rewriting the image creates fresh recovery media; do so
deliberately after reviewing a failed attempt, never by deleting state markers.

Future backups: sudo chromapad-debian-recovery-backup
Follow: sudo journalctl -fu chromapad-debian-recovery-backup.service
That command captures a NEW backup and creates a NEW verified USB image.
The latest completed USB image is output/debian-recovery-latest.img.
Existing backup generations and the stock coprint.img are not overwritten.
Services may briefly stop during the consistent backup copy, then return.
Run only while the printer is idle and Klipper services are stopped.

This image passed offline validation; physical display/boot acceptance for
this new release is recorded separately. Warm restarts and printer acceptance
are not established by image-building tests. Keep these private images private:
they contain your saved settings and SSH identity.
''')
    for p in [output,*output.rglob('*')]:
        b.require(p.resolve().is_relative_to(output.resolve()),'Release path escaped')
        os.chown(p,1000,1000);p.chmod(0o700 if p.is_dir() else 0o600)
    b.command('sync','-f',output)
    anchor=c.BASE/'output/debian-recovery-latest.img'
    b.require(not anchor.exists() or anchor.is_symlink(),'Latest anchor must be a symlink')
    pending=anchor.parent/('.latest-'+m['build_id'])
    pending.symlink_to(m['build_id']+'/'+image.name)
    os.replace(pending,anchor)
    b.command('sync','-f',anchor.parent)
    b.record(stage/'reports/published.json',result)
    print(json.dumps(result,indent=2),flush=True)

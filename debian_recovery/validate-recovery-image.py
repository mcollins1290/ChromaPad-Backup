import json,os,shutil,subprocess,tarfile,tempfile
from pathlib import Path
import recovery_build_common as c
from recovery_build_common import b
import chromapad_recovery as r
def validate(stage,m,image=None):
    pin=json.loads((stage/'payload/release.json').read_text())['manifest_sha256']
    b.require((stage/'reports/config-built.json').exists(),'Configuration incomplete')
    with c.usb_image(stage,image=image) as (fat,root):
        bundle=root/'opt/chromapad-recovery';manifest=r.validate_manifest(bundle,pin)
        # The rolling writer has hashed the clean payload after its last write.
        # Validate its pinned identity here; physical recovery hashes it again.
        if manifest['root_payload'].get('encoding')=='raw':
            b.require(manifest['root_payload']['raw_sha256']==m['backup_sha256'],'Embedded payload pin differs')
        else:
            b.require(b.digest(bundle/'root.ext4.gz')==manifest['root_payload']['compressed_sha256'],'Embedded payload differs')
        b.require(b.digest(bundle/'ram-tools.tar.gz')==manifest['runtime']['sha256'],'Embedded RAM runtime differs')
        r.validate_arm(json.loads((fat/'recovery-arm.json').read_text()),manifest,pin)
        b.require(not (root/'etc/chromapad-emmc').exists(),'Normal eMMC marker present')
        b.require((root/'etc/chromapad-usb').is_file(),'Recovery board-service marker absent')
        lines=[line for line in (root/'etc/fstab').read_text().splitlines() if line and not line.startswith('#')]
        b.require(len(lines)==3 and all('mmc' not in line and 'raid' not in line for line in lines),'Unexpected fstab dependency')
        b.require(lines[0].startswith('UUID='+m['usb_root_uuid']+' / ') and lines[1].startswith('UUID='+m['usb_fat_uuid']+' /boot/recovery '),'Wrong runtime filesystem IDs')
        b.require(' ro,' in lines[1],'Recovery FAT not configured read-only')
        units=['cron','ChromaScreen','nginx','udisks2','crowsnest']+[f'{p}-{i}' for p in ('klipper','moonraker') for i in range(1,9)]
        for unit in units:b.require(os.readlink(root/'etc/systemd/system'/(unit+'.service'))=='/dev/null','Unit not masked: '+unit)
        b.require(os.readlink(root/'etc/systemd/system/multi-user.target.wants/chromapad-recovery.service')=='../chromapad-recovery.service','Recovery service not enabled')
        b.require(os.readlink(root/'etc/systemd/system/multi-user.target.wants/chromapad-recovery-complete.service')=='../chromapad-recovery-complete.service','Completion button service not enabled')
        for name,target in [('recovery_shutdown.py',root/'usr/local/lib/chromapad-recovery/recovery_shutdown.py'),('chromapad-recovery-complete.service',root/'etc/systemd/system/chromapad-recovery-complete.service')]:
            b.require(b.digest(target)==b.digest(c.LOCAL/name),'Shutdown component mismatch: '+name)
        script=(fat/'boot.cmd').read_text()
        for arg in b.USB_ARGS:b.require(script.count(arg)==1,'Missing USB argument: '+arg)
        b.require('root=UUID='+m['usb_root_uuid'] in script and 'chromapad.emmc_probe=readonly' in script and 'chromapad.recovery='+m['build_id'] in script,'Wrong recovery boot arguments')
        b.require('events_poll_msecs}="0"' in (root/'etc/udev/rules.d/99-chromapad-usb-storage-no-media-poll.rules').read_text(),'USB storage polling policy absent')
        r.legacy(fat/'s905_autoscript',6);r.legacy(fat/'uInitrd',3)
        b.command('python3',stage/'inputs/helpers/validate-boot-script.py',fat/'boot.cmd',stage/'reports/final-usb-script.json')
        b.command('python3',stage/'inputs/helpers/validate-boot-memory.py',fat,stage/'reports/final-usb-memory.json')
        b.require((root/'home/cp/.ssh/authorized_keys').is_file(),'SSH authorization missing')
        b.require((root/'var/log/journal').is_dir() and 'Storage=persistent' in (root/'etc/systemd/journald.conf.d/chromapad-recovery.conf').read_text(),'Recovery logs are not persistent')
        b.require(not (root/'var/lib/chromapad-recovery').exists() or not any((root/'var/lib/chromapad-recovery').iterdir()),'Used attempt state in release')
        ram=Path(tempfile.mkdtemp(prefix='chromapad-recovery-runtime-check-'))
        try:
            b.command('mount','-t','tmpfs','-o','size=512M,nosuid','tmpfs',ram)
            try:
                with tarfile.open(bundle/'ram-tools.tar.gz','r:gz') as archive:archive.extractall(ram,filter='data')
                b.require(b.digest(ram/'recovery/chromapad_recovery.py')==manifest['engine_sha256'],'Extracted engine mismatch')
                # The inspected image remains read-only/noexec. Exercise the
                # exact installed shutdown helper in the disposable RAM runtime.
                shutil.copyfile(root/'usr/local/lib/chromapad-recovery/recovery_shutdown.py',ram/'recovery/recovery_shutdown.py')
                with c.emulator(),c.minimal_devices(ram):
                    checks={}
                    for binary,flag in [('python3','--version'),('mount','--version'),('umount','--version'),('findmnt','--version'),('udevadm','--version'),('systemctl','--version'),('sync','--version')]:
                        checks[binary]=c.arm(ram,'/usr/bin/'+binary,flag)
                    for binary,flag in [('blkid','--version'),('blockdev','--version'),('e2fsck','-V'),('fsck.vfat','--help')]:checks[binary]=c.arm(ram,'/usr/sbin/'+binary,flag)
                    checks['engine_imports']=c.arm(ram,'/usr/bin/python3','/recovery/chromapad_recovery.py','--help')
                    checks['display_render']=c.arm(ram,'/usr/bin/python3','-c',"import sys;sys.path.insert(0,'/recovery');import recovery_ui as u;im=u.render({'title':'Restoring Debian','step':4,'done':50,'total':100});assert im.size==(1024,600);print('RAM display rendered')")
                    shutdown_check=c.arm(ram,'/usr/bin/python3','-c',"import sys;sys.path.insert(0,'/recovery');import recovery_shutdown as s;assert s.render().size==(1024,600);print('Shutdown button rendered')")
                    b.record(stage/'reports/shutdown-display-smoke.json',{'status':'PASS','arm_render':shutdown_check,'poweroff_executed':False,'image_mount_read_only_noexec':True})
                    # This environment has no target devices or host mounts. The
                    # physical path must fail before it can change a device.
                    result=subprocess.run(['chroot',str(ram),'/usr/bin/python3','-c',"import sys;sys.path.insert(0,'/recovery');import chromapad_recovery as r;r.physical_target({})"],capture_output=True,text=True)
                    b.require(result.returncode!=0 and 'findmnt' in result.stderr,'RAM toolchain did not reject absent host binding')
                    checks['non_recovery_environment_rejected']=True
                b.record(stage/'reports/arm-runtime-smoke.json',checks)
            finally:b.command('umount',ram)
        finally:ram.rmdir()
    # Check unmounted filesystems without allowing any repair writes.
    for i,(start,sectors) in enumerate(c.USB_PARTS):
        loop=b.run('losetup','--find','--show','--read-only','--offset',start*512,'--sizelimit',sectors*512,image if image is not None else stage/'recovery-usb.img')
        try:
            with (stage/'reports'/('usb-fat-fsck.txt' if i==0 else 'usb-root-fsck.txt')).open('w') as log:
                b.command(*(['fsck.vfat','-n'] if i==0 else ['e2fsck','-f','-n']),loop,stdout=log,stderr=subprocess.STDOUT)
        finally:b.command('losetup','-d',loop)
    result={'status':'PASS','manifest_sha256':pin,'payload_embedded_checksum_verified':True,'automatic_restore_armed':True,'arm_toolchain_smoke_passed':True,'usb_filesystems_clean':True,'physical_boot_tested':False}
    b.record(stage/'reports/image-validation.json',result);b.record(stage/'reports/recovery-image-validation.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    c.private()
    with c.project() as (stage,m):validate(stage,m)

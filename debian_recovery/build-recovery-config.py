#!/usr/bin/python3
import base64,json,os,re,shutil,subprocess
from pathlib import Path
import recovery_build_common as c
from recovery_build_common import b
import chromapad_recovery as r
from package_recovery_runtime import package

def configure(stage,m,image=None,rolling=False):
    b.require((stage/'reports/base-built.json').exists(),'Base incomplete')
    b.require(not (stage/'reports/config-built.json').exists(),'Configuration already built')
    bundle=stage/'payload';boot=bundle/'boot'
    shutil.copytree(stage/'inputs/emmc-boot',boot)
    old=json.loads((boot/'boot-manifest.json').read_text())['generation']
    reuse=bool(re.fullmatch(r'debian-restored-[0-9TZ]+',old))
    gen=old if reuse else m['build_id'].replace('debian-recovery-','debian-restored-')
    if not reuse:
        (boot/'boot.cmd').write_text((boot/'boot.cmd').read_text().replace(old,gen))
        b.command('mkimage','-A','arm','-O','linux','-T','script','-C','none','-n','ChromaPad Debian recovery boot','-d',boot/'boot.cmd',boot/'emmc_autoscript')
    b.command('python3',stage/'inputs/helpers/validate-boot-script.py',boot/'boot.cmd',stage/'reports/restored-script-validation.json')
    # The memory validator uses USB filenames; aliases are temporary only.
    shutil.copy2(boot/'meson-sm1-chromapad-emmc.dtb',boot/'meson-sm1-chromapad.dtb')
    shutil.copy2(boot/'emmc_autoscript',boot/'s905_autoscript')
    b.command('python3',stage/'inputs/helpers/validate-boot-memory.py',boot,stage/'reports/restored-memory-validation.json')
    (boot/'meson-sm1-chromapad.dtb').unlink();(boot/'s905_autoscript').unlink()
    files=lambda folder:{p.name:{'bytes':p.stat().st_size,'sha256':b.digest(p)} for p in folder.iterdir() if p.is_file() and p.name!='boot-manifest.json'}
    if not reuse:b.record(boot/'boot-manifest.json',{'generation':gen,'root_uuid':r.ROOT_UUID,'fat_uuid':r.FAT_UUID,'kernel':m['kernel'],'files':files(boot)})
    for name in ('Image','uInitrd','meson-sm1-chromapad-emmc.dtb'):
        b.require(b.digest(boot/name)==b.digest(stage/'inputs/emmc-boot'/name),'Installed boot binary changed')
    evidence=json.loads((c.LOCAL/'emmc-readiness-evidence.json').read_text(encoding='utf-8-sig'))
    (bundle/'target-mbr.bin').write_bytes(base64.b64decode(evidence['saved_mbr_base64']))
    with c.usb_image(stage,True,image=image) as (fat,root):
        b.phase(stage,'Build guarded recovery initramfs in the offline ARM runtime')
        with c.scratch(root) as work:
            for source,name in [(stage/'inputs/initrd.raw','initrd.raw'),((stage if rolling else c.LOCAL)/'recovery-emmc-guard.sh','recovery-emmc-guard.sh'),(c.LOCAL/'build_recovery_initramfs_arm.py','build.py')]:shutil.copyfile(source,work/name)
            with c.emulator(),c.minimal_devices(root):
                result=c.arm(root,'/usr/bin/python3','/var/tmp/recovery-build/build.py')
                (stage/'reports/initramfs-build.txt').write_text(result+'\n')
            shutil.copy2(work/'initramfs-report.json',stage/'reports/initramfs-report.json')
            b.command('mkimage','-A','arm64','-O','linux','-T','ramdisk','-C','gzip','-a','0','-e','0','-n','ChromaPad protected recovery','-d',work/'guarded-initrd.raw',fat/'uInitrd')
        shutil.copy2(boot/'Image',fat/'Image');shutil.copy2(boot/'meson-sm1-chromapad-emmc.dtb',fat/'meson-sm1-chromapad.dtb')
        args='root=UUID='+m['usb_root_uuid']+' rootfstype=ext4 rootwait rw fsck.repair=preen console=ttyAML0,115200n8 console=tty1 consoleblank=0 net.ifnames=0 panic=0 '+' '.join(b.USB_ARGS)+' chromapad.emmc_probe=readonly chromapad.recovery='+m['build_id']+' systemd.gpt_auto=no rd.systemd.gpt_auto=no noresume'
        script=(stage/'inputs/usb-boot.cmd').read_text()
        script=re.sub(r"setenv bootargs '[^']*'","setenv bootargs '"+args+"'",script)
        for name in ('Image','uInitrd','meson-sm1-chromapad.dtb'):
            pattern=r'(if fatload usb \$\{usbdev\}:1 \$\{[^}]+\} '+re.escape(name)+r' )([0-9a-f]+)(; then\n\s*if test \$\{filesize\} = )\2'
            script,count=re.subn(pattern,lambda x:x[1]+format((fat/name).stat().st_size,'x')+x[3]+format((fat/name).stat().st_size,'x'),script)
            b.require(count==1,'Unexpected USB boot template')
        (fat/'boot.cmd').write_text(script)
        b.command('mkimage','-A','arm','-O','linux','-T','script','-C','none','-n','ChromaPad protected recovery','-d',fat/'boot.cmd',fat/'s905_autoscript')
        b.command('python3',stage/'inputs/helpers/validate-boot-script.py',fat/'boot.cmd',stage/'reports/usb-script-validation.json')
        b.command('python3',stage/'inputs/helpers/validate-boot-memory.py',fat,stage/'reports/usb-memory-validation.json')
        b.require(b.run('fdtget','-t','s',fat/'meson-sm1-chromapad.dtb','/soc/mmc@ffe07000','status')=='okay','MMC unavailable to guard')
        b.phase(stage,'Isolate services and build the recovery RAM toolchain')
        (root/'etc/fstab').write_text('# Dedicated recovery USB: no eMMC or NAS mounts\nUUID='+m['usb_root_uuid']+' / ext4 defaults,noatime 0 1\nUUID='+m['usb_fat_uuid']+' /boot/recovery vfat ro,nosuid,nodev,noexec,umask=0077 0 2\ntmpfs /tmp tmpfs nosuid,nodev,mode=1777 0 0\n')
        (root/'boot/recovery').mkdir(exist_ok=True)
        # Backups intentionally omit old logs. Recovery must recreate persistent
        # journal storage, including for failures before an attempt marker exists.
        journal=root/'var/log/journal';journal.mkdir(parents=True,exist_ok=True);journal.chmod(0o2755)
        group=next(line.split(':')[2] for line in (root/'etc/group').read_text().splitlines() if line.startswith('systemd-journal:'))
        os.chown(journal,0,int(group))
        confdir=root/'etc/systemd/journald.conf.d';confdir.mkdir(exist_ok=True)
        (confdir/'chromapad-recovery.conf').write_text('[Journal]\nStorage=persistent\nSystemMaxUse=64M\nRuntimeMaxUse=16M\nSyncIntervalSec=5s\n')
        conf=root/'etc/chromapad-emmc'
        if conf.is_dir():shutil.rmtree(conf)
        elif conf.exists():conf.unlink()
        (root/'etc/chromapad-usb').write_text('Dedicated protected Debian recovery USB; automatic restore to the pinned pad.\n')
        units=['cron.service','ChromaScreen.service','crowsnest.service','nginx.service','udisks2.service','packagekit.service','apt-daily.service','apt-daily-upgrade.service','apt-daily.timer','apt-daily-upgrade.timer','fstrim.timer','e2scrub_all.timer','e2scrub_reap.service','sleep.target','suspend.target','hibernate.target','hybrid-sleep.target','getty@tty1.service','chromapad-debian-backup.service','chromapad-debian-recovery-backup.service','chromapad-backup.service']+[f'{prefix}-{i}.service' for prefix in ('klipper','moonraker') for i in range(1,9)]
        for unit in units:
            p=root/'etc/systemd/system'/unit
            if p.exists() or p.is_symlink():p.unlink()
            p.symlink_to('/dev/null')
        p=root/'etc/systemd/system/default.target'
        if p.exists() or p.is_symlink():p.unlink()
        p.symlink_to('/usr/lib/systemd/system/multi-user.target')
        runtime=package(root,stage,c.LOCAL/'chromapad_recovery.py')
        b.record(stage/'reports/ram-runtime.json',runtime)
        shutil.copy2(stage/'ram-tools.tar.gz',bundle/'ram-tools.tar.gz')
        ref=json.loads((c.LOCAL/'protected-reference.json').read_text(encoding='utf-8-sig'))
        # Reference file shape is checked explicitly below, never inferred loosely.
        b.require(set(ref)=={'0:71303168','607125504:1684013056','boot0','boot1'},'Unexpected protected reference structure')
        manifest={'schema':2 if rolling else 1,'kind':'chromapad-usb-recovery','build_id':m['build_id'],'target':{'cid':r.CID,'bytes':r.SIZE,'sector_bytes':512,'root_uuid':r.ROOT_UUID,'fat_uuid':r.FAT_UUID,'mbr_sha256':r.MBR_SHA,'partitions':[list(x) for x in r.PARTS],'protected_ranges':[list(x) for x in r.RANGES]},'usb_root_uuid':m['usb_root_uuid'],'usb_fat_uuid':m['usb_fat_uuid'],'backup_sha256':m['backup_sha256'],'boot_generation':gen,'engine_sha256':b.digest(c.LOCAL/'chromapad_recovery.py'),'root_payload':{'file':'root.ext4.gz',**json.loads((stage/'reports/root-payload.json').read_text())},'protected_reference':ref,'runtime':{'file':'ram-tools.tar.gz','sha256':b.digest(bundle/'ram-tools.tar.gz'),**runtime},'boot_files':{p.name:{'bytes':p.stat().st_size,'sha256':b.digest(p)} for p in boot.iterdir()}}
        manifest['display_sha256']=b.digest(c.LOCAL/'recovery_ui.py')
        b.record(bundle/'manifest.json',manifest)
        pin=b.digest(bundle/'manifest.json')
        b.record(bundle/'release.json',{'manifest_sha256':pin,'default_mode':'automatic-restore'})
        destination=root/'opt/chromapad-recovery'
        if rolling:
            # The sparse root payload already lives inside this image.
            shutil.copytree(bundle,destination,dirs_exist_ok=True)
        else:
            r.validate_manifest(bundle,pin)
            shutil.copytree(bundle,destination)
        r.validate_manifest(destination,pin)
        (root/'var/lib/chromapad-recovery').mkdir(mode=0o700,exist_ok=True)
        shutil.copy2(c.LOCAL/'recovery_ram_launcher.py',root/'usr/local/sbin/chromapad-recovery-launcher');(root/'usr/local/sbin/chromapad-recovery-launcher').chmod(0o755)
        (root/'usr/local/lib/chromapad-recovery').mkdir(parents=True,exist_ok=True)
        shutil.copy2(c.LOCAL/'recovery_ui.py',root/'usr/local/lib/chromapad-recovery/recovery_ui.py')
        shutil.copy2(c.LOCAL/'recovery_shutdown.py',root/'usr/local/lib/chromapad-recovery/recovery_shutdown.py')
        shutil.copy2(c.LOCAL/'chromapad-recovery-complete.service',root/'etc/systemd/system/chromapad-recovery-complete.service')
        (root/'etc/systemd/system/multi-user.target.wants/chromapad-recovery-complete.service').symlink_to('../chromapad-recovery-complete.service')
        (root/'etc/systemd/system/chromapad-recovery.service').write_text('[Unit]\nDescription=ChromaPad protected recovery and read-only probe\nAfter=local-fs.target systemd-udev-settle.service\nWants=systemd-udev-settle.service\nRequiresMountsFor=/boot/recovery /opt/chromapad-recovery /var/lib/chromapad-recovery\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/sbin/chromapad-recovery-launcher\nRemainAfterExit=yes\nTimeoutStartSec=infinity\nTimeoutStopSec=120\nOOMScoreAdjust=-900\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n')
        (root/'etc/systemd/system/multi-user.target.wants/chromapad-recovery.service').symlink_to('../chromapad-recovery.service')
        (root/'etc/motd').write_text('ChromaPad Debian recovery USB. AUTOMATIC RESTORE with on-screen progress.\nStatus: systemctl status chromapad-recovery\nLogs: journalctl -u chromapad-recovery\nPersistent results: /var/lib/chromapad-recovery\nDo not mount eMMC or restart recovery. Keep USB connected until shutdown.\n')
        b.record(fat/'recovery-release.json',{'build_id':m['build_id'],'manifest_sha256':pin,'mode':'AUTOMATIC_RESTORE'})
        b.require(not (fat/'recovery-arm.json').exists(),'Default image unexpectedly armed')
        arm={'schema':1,'action':'restore-emmc','cid':r.CID,'build_id':m['build_id'],'manifest_sha256':pin,'confirmation':r.arm_token(manifest,pin)}
        r.validate_arm(arm,manifest,pin)
        b.record(fat/'recovery-arm.json',arm)
        (fat/'AUTOMATIC-RESTORE.txt').write_text('This USB automatically restores Debian to the pinned ChromaPad eMMC.\nKeep power connected until the large completion screen appears.\nThen shut down, remove USB and cold-start from eMMC.\n')
        b.command('sync','-f',root);b.command('sync','-f',fat)
    shutil.copy2(c.LOCAL/'chromapad_recovery.py',stage/'chromapad_recovery.py')
    b.record(stage/'reports/config-built.json',{'status':'PASS','manifest_sha256':pin,'build_id':m['build_id'],'physical_boot_tested':False})
    if not rolling:b.record(c.LOCAL/'recovery-release.json',{'stage':str(stage),'build_id':m['build_id'],'manifest_sha256':pin})
    print('Recovery image configured; offline validation and full restore rehearsal still required',flush=True)


if __name__=='__main__':
    c.private()
    with c.project() as (stage,m):configure(stage,m)

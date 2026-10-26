#!/usr/bin/python3
"""One command: capture Debian eMMC, build and verify an automatic recovery USB image."""
import argparse, datetime, fcntl, hashlib, json, os, shutil, subprocess, sys, tempfile, uuid
from pathlib import Path
import chromapad_debian_backup as b
import recovery_build_common as c

STEPS=('prepare-recovery-inputs.py','prepare-recovery-guard.py','build-recovery-base.py','build-recovery-config.py','audit-recovery-layout.py','validate-recovery-image.py','smoke-recovery-launcher.py','publish-recovery-image.py')

def verify_tools(source):
    rec=json.loads((source/'tool-manifest.json').read_text())
    b.require(rec['kind']=='chromapad-recovery-builder-tools','Unexpected tool manifest')
    for name,checksum in rec['files'].items():
        path=source/name
        b.require(not Path(name).is_absolute() and '..' not in Path(name).parts and path.is_file() and not path.is_symlink(),'Unsafe builder file')
        b.require(b.digest(path)==checksum,'Builder file changed: '+name)
    return rec

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--capture',action='store_true')
    mode.add_argument('--from-backup',type=Path)
    mode.add_argument('--plan',action='store_true')
    parser.add_argument('--private',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    b.require(os.geteuid()==0,'Root required')
    # Let systemd's NAS automount resolve in its original namespace first.
    # A newly mounted NAS cannot propagate into an already-private namespace.
    b.check_destination(b.BASE)
    c.private()
    b.check_destination(b.BASE)
    source=Path(__file__).resolve().parent
    tools=verify_tools(source)
    for command in ('rsync','mkimage','fdtget','readelf','losetup','mount','unshare','cpio','gzip','fsck.vfat','e2fsck','mkfs.vfat','mkfs.ext4'):
        b.require(shutil.which(command),'Missing builder dependency: '+command)
    if args.plan:
        print(json.dumps({'status':'PLAN_ONLY','source':b.source_identity(),'output':str(c.BASE/'output/debian-recovery-latest.img'),'automatic_restore_when_booted':True,'writes_performed':False},indent=2));return
    (c.BASE/'experiments').mkdir(exist_ok=True)
    (c.BASE/'output').mkdir(exist_ok=True)
    with (c.BASE/'.recovery-backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Snapshot the tested builder before capture so a running build cannot
        # silently switch implementations if the installed tool is upgraded.
        build_id='debian-recovery-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        stage=Path(tempfile.mkdtemp(prefix=build_id+'-',dir=c.BASE/'experiments'))
        for folder in ('reports','payload','inputs'):(stage/folder).mkdir()
        job=stage/'tools';shutil.copytree(source,job,ignore=shutil.ignore_patterns('__pycache__'))
        b.record(stage/'status.json',{'state':'RUNNING','phase':'Capturing Debian' if args.capture else 'Using sealed backup'})
        try:
            b.require(shutil.disk_usage(stage).free>45*1024**3,'Need at least 45 GiB free on NAS for a new backup and build')
            if args.capture:
                # capture() validates the live source and restores application
                # service state even on handled errors. It never writes eMMC.
                backup=b.capture()
            else:backup=args.from_backup.resolve(strict=True)
            b.require(backup.parent==b.BASE.resolve() and backup.name.startswith('debian-emmc-'),'Use a sealed Debian backup directory')
            manifest=json.loads((backup/'manifest.json').read_text())
            b.require(manifest['kind']=='chromapad-debian-emmc-backup' and manifest['application_copy_verified'],'Backup is not verified')
            ref=json.loads((job/'protected-reference.json').read_text())
            b.require(manifest['protected_regions']=={key:value for key,value in ref.items() if ':' in key},'Backup protected reference changed')
            for name in ('boot0','boot1'):b.require(manifest['files'][name+'.bin']['sha256']==ref[name],'Hardware boot reference changed')
            meta={'stage':str(stage),'build_id':build_id,'backup_image':str(backup/'debian-emmc.img'),'backup_sha256':manifest['files']['debian-emmc.img']['sha256'],'usb_root_uuid':str(uuid.uuid4()),'usb_fat_uuid':uuid.uuid4().hex[:8].upper(),'root_uuid':b.ROOT_UUID,'fat_uuid':b.FAT_UUID,'cid':b.CID,'usb_prefix_sha256':tools['files']['assets/usb-prefix.bin']}
            b.record(job/'recovery-usb-stage.json',meta)
            for script in ('test_debian_backup.py','test-recovery-engine.py','test-recovery-ui.py','test-recovery-shutdown.py','test-workflow-control.py'):
                with (stage/'reports'/(script+'.log')).open('w') as log:b.command('python3',job/script,stdout=log,stderr=subprocess.STDOUT)
            for script in STEPS:
                print(b.stamp()+' BUILD STEP '+script,flush=True)
                b.phase(stage,script)
                with (stage/'reports'/(script+'.log')).open('w') as log:
                    b.command('python3',job/script,stdout=log,stderr=subprocess.STDOUT)
            result=json.loads((stage/'reports/published.json').read_text())
            b.record(stage/'status.json',{'state':'COMPLETE',**result})
            print(json.dumps(result,indent=2),flush=True)
        except BaseException as exc:
            b.record(stage/'status.json',{'state':'FAILED','error':str(exc),'utc':b.stamp(),'previous_releases_unchanged':True})
            print('Build failed. See '+str(stage/'reports')+'. Previous recovery image remains available.',flush=True)
            raise

if __name__=='__main__':main()

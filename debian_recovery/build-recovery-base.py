#!/usr/bin/env python3
import gzip,hashlib,json,os,shutil,struct,subprocess,uuid
from pathlib import Path
import recovery_build_common as c
from recovery_build_common import b

c.private()
with c.project() as (stage,m):
    b.require(not (stage/'reports/base-built.json').exists() and not (stage/'recovery-usb.img').exists(),'Base already built or incomplete; inspect first')
    m['usb_fat_uuid']=m['usb_fat_uuid'].replace('-','')
    m['usb_fat_uuid']=m['usb_fat_uuid'][:4]+'-'+m['usb_fat_uuid'][4:]
    m['usb_disk_id']=uuid.uuid4().hex[:8]
    b.record(stage/'stage.json',m);b.record(c.LOCAL/'recovery-usb-stage.json',m)
    b.phase(stage,'Verify backup and pinned USB boot prefix')
    b.require(b.digest(m['backup_image'])==m['backup_sha256'],'Backup checksum mismatch')
    b.require(b.digest(stage/'inputs/usb-prefix.bin')==m['usb_prefix_sha256'],'USB prefix checksum mismatch')
    b.record(stage/'reports/source-hashes.json',{'backup':m['backup_sha256'],'usb_prefix':m['usb_prefix_sha256'],'status':'PASS'})
    b.phase(stage,'Compress the exact backed-up root partition into a self-contained payload')
    payload=stage/'payload/root.ext4.gz';remaining=b.PARTS[1][1]*512;raw_sha=hashlib.sha256()
    with open(m['backup_image'],'rb') as src,payload.open('xb') as out:
        src.seek(b.PARTS[1][0]*512)
        with gzip.GzipFile(filename='',mode='wb',fileobj=out,compresslevel=1,mtime=0) as gz:
            while remaining:
                block=src.read(min(4*1024*1024,remaining));b.require(bool(block),'Short backup partition')
                raw_sha.update(block);gz.write(block);remaining-=len(block)
        out.flush();os.fsync(out.fileno())
    record={'compressed_bytes':payload.stat().st_size,'compressed_sha256':b.digest(payload),'raw_bytes':b.PARTS[1][1]*512,'raw_sha256':raw_sha.hexdigest()}
    b.record(stage/'reports/root-payload.json',record)
    b.phase(stage,'Create a fresh USB-layout image with unique filesystem identities')
    image=stage/'recovery-usb.img'
    prefix=bytearray((stage/'inputs/usb-prefix.bin').read_bytes())
    struct.pack_into('<II',prefix,470,*c.USB_PARTS[1])
    struct.pack_into('<I',prefix,440,int(m['usb_disk_id'],16))
    with image.open('xb') as f:f.truncate(c.USB_BYTES);f.write(prefix)
    for i,(start,sectors) in enumerate(c.USB_PARTS):
        loop=b.run('losetup','--find','--show','--offset',start*512,'--sizelimit',sectors*512,image)
        try:
            b.require(b.run('losetup','-n','-O','BACK-FILE',loop)==str(image),'Wrong format loop')
            if i==0:b.command('mkfs.vfat','-F','32','-i',m['usb_fat_uuid'].replace('-',''),'-n','CP_RECOVERY',loop)
            else:b.command('mkfs.ext4','-F','-E','nodiscard,lazy_itable_init=0,lazy_journal_init=0','-U',m['usb_root_uuid'],'-L','CP_RECOVERY','-m','1',loop)
        finally:b.command('losetup','-d',loop)
    source_work=stage/'source';source_work.mkdir()
    with b.mounted_image(Path(m['backup_image']),source_work) as (source_fat,source_root),c.usb_image(stage,True) as (fat,root):
        b.phase(stage,'Copy the verified Debian root into the isolated recovery runtime')
        with (stage/'reports/runtime-copy.txt').open('w') as log:
            b.command('rsync','-aHAXx','--numeric-ids','--exclude=/lost+found',str(source_root)+'/',str(root)+'/',stdout=log,stderr=subprocess.STDOUT)
        difference=b.run('rsync','-aHAXxnc','--numeric-ids','--delete','--itemize-changes','--exclude=/lost+found',str(source_root)+'/',str(root)+'/')
        (stage/'reports/runtime-source-comparison.txt').write_text(difference+'\n');b.require(not difference,'Runtime clone differs from backup')
        # Remove application state only from the disposable USB runtime; the
        # complete, checksummed restoration payload above retains all of it.
        keep={'.ssh','.bashrc','.profile','.bash_logout'}
        removed=[]
        for p in (root/'home/cp').iterdir():
            if p.name in keep:continue
            b.require(p.parent==root/'home/cp','Unsafe runtime trim')
            removed.append(p.name)
            if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
            else:p.unlink()
        b.record(stage/'reports/runtime-trim.json',{'removed_from_runtime_home_only':removed,'payload_unchanged':True})
        b.require(shutil.disk_usage(root).free>payload.stat().st_size+1024**3,'Insufficient USB runtime space including FAT backup reserve')
        # Copy helper scripts for inspection; no service from this clone is run.
        shutil.copyfile(root/'usr/bin/unmkinitramfs',stage/'inputs/unmkinitramfs.sh')
        b.command('sync','-f',root)
    b.record(stage/'reports/base-built.json',{'status':'PASS','payload':record,'usb_image_bytes':c.USB_BYTES,'image_bootable_or_published':False})
    b.phase(stage,'Recovery base prepared; runtime isolation, guard and restore engine still to be installed')
    print(json.dumps({'status':'BASE_READY','stage':str(stage),'payload':record},indent=2),flush=True)

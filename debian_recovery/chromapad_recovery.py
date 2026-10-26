#!/usr/bin/env python3
"""Pinned ChromaPad recovery engine. Physical execution requires the RAM runtime.

An unarmed USB only probes. --rehearse creates its own disposable file target.
Firmware, partition tables and eMMC hardware boot areas are never write targets.
"""
import argparse, contextlib, datetime, fcntl, gzip, hashlib, json, mmap, os
from pathlib import Path
import re, shutil, signal, stat, struct, subprocess, sys, tempfile, uuid, zlib
from recovery_ui import RecoveryScreen

CID='150100414a4e4234520735a5fac66300'
SIZE=15634268160
PARTS=[(139264,1046528,'c'),(3289088,27246592,'83')]
RANGES=[(0,71303168),(607125504,1684013056)]
ROOT_UUID='89ebf872-79e9-488b-84d7-5ffb49263c0e'
FAT_UUID='B1C3-F827'
MBR_SHA='e7ac296d23ef572ae908c377af0289ef0a7a355bd4603e0fd66e85d1fee2b60b'
RAW_BYTES=PARTS[1][1]*512
CHUNK=4*1024*1024
SCREEN=False
DISPLAY=RecoveryScreen(enabled=False)

def require(ok,message):
    if not ok:raise RuntimeError(message)

def run(*args,timeout=60):
    return subprocess.check_output([str(a) for a in args],text=True,timeout=timeout).strip()

def command(*args,**kwargs):
    subprocess.run([str(a) for a in args],check=True,**kwargs)

def sha(path,progress=False):
    h=hashlib.sha256()
    total=Path(path).stat().st_size;done=0
    with Path(path).open('rb',buffering=0) as f:
        while data:=f.read(CHUNK):
            h.update(data);done+=len(data)
            if progress:DISPLAY.update('Checking the backup',1,done,total,'Verifying the compressed backup file.')
    return h.hexdigest()

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()

def report(message):
    text=utc()+' ChromaPad recovery: '+message
    print(text,flush=True)
    if SCREEN:
        try:
            with open('/dev/tty1','w') as tty:tty.write(text+'\n')
        except OSError:pass

def save(path,value,exclusive=False):
    path=Path(path)
    require(not path.is_symlink(),'Refusing symlink state file')
    raw=(json.dumps(value,indent=2)+'\n').encode()
    with path.open('xb' if exclusive else 'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def region_hash(path,start,length,progress=False):
    h=hashlib.sha256()
    total=length;done=0
    if stat.S_ISBLK(os.stat(path).st_mode):
        require(start%512==0 and length%512==0,'Unaligned direct read')
        fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_DIRECT);buf=mmap.mmap(-1,CHUNK)
        try:
            for offset in range(start,start+length,CHUNK):
                view=memoryview(buf)[:min(CHUNK,start+length-offset)]
                try:
                    require(os.preadv(fd,[view],offset)==len(view),'Short device read');h.update(view);done+=len(view)
                    if progress:DISPLAY.update('Verifying the restore',5,done,total,'Reading back every byte from eMMC.')
                finally:view.release()
        finally:buf.close();os.close(fd)
    else:
        with open(path,'rb',buffering=0) as f:
            f.seek(start)
            while length:
                data=f.read(min(CHUNK,length));require(bool(data),'Short file read');h.update(data);length-=len(data);done+=len(data)
                if progress:DISPLAY.update('Verifying the restore',5,done,total,'Reading back every byte from eMMC.')
    return h.hexdigest()

def legacy(path,kind):
    data=Path(path).read_bytes();require(len(data)>=64,'Short U-Boot image')
    magic,hcrc,stamp,size,load,entry,dcrc=struct.unpack('>7I',data[:28]);head=bytearray(data[:64]);head[4:8]=bytes(4)
    require(magic==0x27051956 and size==len(data)-64 and zlib.crc32(head)==hcrc and zlib.crc32(data[64:])==dcrc and data[30]==kind,'Invalid U-Boot image')

def payload_spec(m):
    """Read old gzip releases and the rolling image's sparse raw filesystem."""
    p=m['root_payload']
    encoding=p.get('encoding','gzip')
    if encoding=='raw':
        require(m['schema']==2 and p['file']=='root.ext4','Wrong raw payload format')
        require(p['bytes']==RAW_BYTES and p['sha256']==p['raw_sha256'],'Wrong raw payload identity')
        return p['file'],p['bytes'],p['sha256'],open
    require(encoding=='gzip' and p['file']=='root.ext4.gz','Wrong compressed payload format')
    return p['file'],p['compressed_bytes'],p['compressed_sha256'],gzip.open

def validate_manifest(bundle,expected):
    bundle=Path(bundle)
    require(bundle.is_dir() and not bundle.is_symlink(),'Unsafe payload directory')
    require(re.fullmatch('[0-9a-f]{64}',expected or '') and sha(bundle/'manifest.json')==expected,'Manifest pin mismatch')
    m=json.loads((bundle/'manifest.json').read_text())
    require(m['schema'] in (1,2) and m['kind']=='chromapad-usb-recovery','Wrong recovery format')
    require(re.fullmatch(r'debian-recovery-[0-9TZ]+',m['build_id']) is not None,'Unsafe build ID')
    require(m['target']=={'cid':CID,'bytes':SIZE,'sector_bytes':512,'root_uuid':ROOT_UUID,'fat_uuid':FAT_UUID,
                         'mbr_sha256':MBR_SHA,'partitions':[list(x) for x in PARTS],'protected_ranges':[list(x) for x in RANGES]},'Recovery write scope changed')
    require(m['usb_root_uuid'] not in (ROOT_UUID,'38327d56-529b-498f-8b1a-e81b534bfeb9'),'USB root UUID collision')
    require(str(uuid.UUID(m['usb_root_uuid']))==m['usb_root_uuid'],'Malformed USB UUID')
    require(re.fullmatch(r'[0-9A-F]{4}-[0-9A-F]{4}',m['usb_fat_uuid']) and m['usb_fat_uuid']!=FAT_UUID,'Bad USB FAT UUID')
    require(m['root_payload']['raw_bytes']==RAW_BYTES,'Wrong root payload size')
    payload_name,payload_bytes,_,_=payload_spec(m)
    require(re.fullmatch(r'debian-restored-[0-9TZ]+',m['boot_generation']) is not None,'Unsafe boot generation')
    require(sha(Path(__file__))==m['engine_sha256'],'Recovery engine does not match manifest')
    require(sha(Path(__file__).with_name('recovery_ui.py'))==m['display_sha256'],'Recovery display does not match manifest')
    required={'Image','uInitrd','meson-sm1-chromapad-emmc.dtb','emmc_autoscript','boot.cmd','boot-manifest.json'}
    require(required<=set(m['boot_files']),'Incomplete boot bundle')
    for name,rec in m['boot_files'].items():
        require(Path(name).name==name and name not in ('.','..'),'Unsafe boot filename')
        p=bundle/'boot'/name
        require(p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(bundle.resolve()),'Unsafe boot asset')
        require(p.stat().st_size==rec['bytes'] and sha(p)==rec['sha256'],'Boot asset mismatch: '+name)
    legacy(bundle/'boot/uInitrd',3);legacy(bundle/'boot/emmc_autoscript',6)
    script=(bundle/'boot/boot.cmd').read_text()
    require('root=UUID='+ROOT_UUID in script and 'debian/'+m['boot_generation']+'/' in script,'Wrong installed boot references')
    require('fatload usb' not in script and 'autoflash=' not in script,'Unsafe installed boot command')
    payload=bundle/payload_name
    require(payload.is_file() and not payload.is_symlink() and payload.stat().st_size==payload_bytes,'Invalid root payload')
    return m

def check_superblock(data):
    require(len(data)>=2048,'Missing ext4 superblock')
    sb=data[1024:2048]
    require(struct.unpack_from('<H',sb,56)[0]==0xef53,'Payload is not ext4')
    require(str(uuid.UUID(bytes=sb[104:120]))==ROOT_UUID,'Payload root UUID mismatch')
    require(struct.unpack_from('<H',sb,58)[0]&1,'Payload filesystem not clean')
    blocks=struct.unpack_from('<I',sb,4)[0]
    if struct.unpack_from('<I',sb,96)[0]&0x80:blocks|=struct.unpack_from('<I',sb,336)[0]<<32
    require(blocks*(1024<<struct.unpack_from('<I',sb,24)[0])==RAW_BYTES,'Payload filesystem geometry mismatch')

def stream_payload(bundle,m,destination=None):
    total=0;h=hashlib.sha256();next_progress=1024**3
    name,_,_,reader=payload_spec(m)
    with reader(Path(bundle)/name,'rb') as src:
        while data:=src.read(CHUNK):
            if total==0:check_superblock(data)
            total+=len(data);require(total<=RAW_BYTES,'Expanded payload exceeds root partition')
            h.update(data)
            if destination is not None:
                view=memoryview(data)
                while view:
                    wrote=os.write(destination,view);require(wrote>0,'Short root write');view=view[wrote:]
                if total>=next_progress:report('Root write '+str(total)+' / '+str(RAW_BYTES)+' bytes');next_progress+=1024**3
            DISPLAY.update('Restoring Debian' if destination is not None else 'Checking the backup',4 if destination is not None else 2,total,RAW_BYTES,'Copying your saved system to eMMC.' if destination is not None else 'Verifying the complete Debian filesystem.')
    require(total==RAW_BYTES and h.hexdigest()==m['root_payload']['raw_sha256'],'Expanded payload checksum/length mismatch')
    return h.hexdigest()

def validate_payload(bundle,m):
    report('Checking the complete saved root before any target writes')
    DISPLAY.update('Checking the backup',1,detail='Verifying the saved filesystem.')
    name,_,checksum,_=payload_spec(m)
    if m['root_payload'].get('encoding','gzip')=='gzip':
        require(sha(Path(bundle)/name,progress=True)==checksum,'Compressed payload checksum mismatch')
    stream_payload(bundle,m)

def arm_token(m,manifest_sha):return 'RESTORE-DEBIAN:'+CID+':'+m['build_id']+':'+manifest_sha

def validate_arm(value,m,manifest_sha):
    require(value=={'schema':1,'action':'restore-emmc','cid':CID,'build_id':m['build_id'],
                    'manifest_sha256':manifest_sha,'confirmation':arm_token(m,manifest_sha)},'USB is not explicitly armed for this exact pad and backup')

def check_unmounted(names):
    devs={(Path('/sys/class/block')/name/'dev').read_text().strip() for name in names}
    for p in Path('/proc').glob('[0-9]*/mountinfo'):
        try:lines=p.read_text().splitlines()
        except (FileNotFoundError,ProcessLookupError):continue
        require(not any(line.split()[2] in devs for line in lines),'Target is mounted in a process namespace')
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        require(Path(line.split()[0]).resolve().name not in names,'Target is swap')
    for name in names:require(not list((Path('/sys/class/block')/name/'holders').iterdir()),'Target has block holders')

def target_geometry(device,fixture=False):
    require(stat.S_ISBLK(os.stat(device).st_mode),'Target must be a validated block device')
    require(run('blockdev','--getsize64',device)==str(SIZE) and run('blockdev','--getss',device)=='512','Target geometry mismatch')
    require(region_hash(device,0,512)==MBR_SHA,'Target partition table/MBR differs')
    name=Path(device).name
    for i,(start,size,kind) in enumerate(PARTS,1):
        p=Path('/sys/class/block')/(name+'p'+str(i))
        require(int((p/'start').read_text())==start and int((p/'size').read_text())==size,'Target partition boundary mismatch')
    result=subprocess.run(['blkid','-p','-c','/dev/null','-o','export',device+'p1'],capture_output=True,text=True)
    info=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    require(result.returncode==0 and info.get('UUID')==FAT_UUID and info.get('TYPE')=='vfat','Existing FAT needs separate recovery; refusing to format it')

def physical_target(m):
    # Check the boot source and RAM environment BEFORE any target state changes.
    require(run('findmnt','-n','-o','FSTYPE','--mountpoint','/')=='tmpfs','Recovery toolchain must run from RAM')
    require(Path('/recovery/runtime.marker').read_text().strip()=='ChromaPad recovery RAM tools v1','Wrong RAM runtime')
    require(run('findmnt','-n','-o','UUID','--mountpoint','/host/root')==m['usb_root_uuid'],'This is not the recovery USB root')
    require('ro' in run('findmnt','-n','-o','OPTIONS','--mountpoint','/host/root').split(','),'Host root alias is not read-only')
    usb=run('findmnt','-n','-o','SOURCE','--mountpoint','/host/root')
    require(re.fullmatch(r'/dev/sd[a-z]+2',usb) is not None,'Recovery root is not a USB partition')
    require('/usb' in str((Path('/sys/class/block')/Path(usb[:-1]).name).resolve()),'Recovery source not on USB bus')
    require(run('findmnt','-n','-o','UUID','--mountpoint','/usb-boot')==m['usb_fat_uuid'],'Wrong recovery FAT')
    require(run('findmnt','-n','-o','SOURCE','--mountpoint','/usb-boot')==usb[:-1]+'1','Recovery FAT and root are not on the same USB')
    require('ro' in run('findmnt','-n','-o','OPTIONS','--mountpoint','/usb-boot').split(','),'Recovery FAT is not read-only')
    require(run('findmnt','-n','-o','UUID','--target','/state')==m['usb_root_uuid'],'Attempt state must be on this USB root')
    args=Path('/proc/cmdline').read_text().split()
    require('root=UUID='+m['usb_root_uuid'] in args and 'chromapad.emmc_probe=readonly' in args and 'chromapad.recovery='+m['build_id'] in args,'Recovery boot flags absent')
    require(not any(a.startswith('autoflash=') for a in args),'Vendor recovery trigger forbidden')
    guard=Path('/run/initramfs/chromapad-emmc-probe.log').read_text()
    require('PASS protect' in guard and 'PASS verify' in guard and CID in guard and 'FAIL' not in guard,'Early protection guard did not pass')
    matches=[p for p in Path('/sys/block').glob('mmcblk*') if (p/'device/cid').is_file() and (p/'device/cid').read_text().strip()==CID]
    require(len(matches)==1,'Pinned eMMC absent or ambiguous')
    disk=matches[0];device='/dev/'+disk.name
    require('ffe07000.mmc' in str((disk/'device').resolve()),'Wrong target controller')
    for key,value in [('name','AJNB4R'),('serial','0x35a5fac6'),('type','MMC')]:require((disk/'device'/key).read_text().strip()==value,'Wrong eMMC '+key)
    names=[disk.name+x for x in ('','p1','p2','boot0','boot1')]
    require(sorted(p.name for p in Path('/sys/class/block').glob('mmcblk*'))==sorted(names),'Unexpected MMC surfaces')
    check_unmounted(names)
    for name in names:require(run('blockdev','--getro','/dev/'+name)=='1','Target protection missing')
    for name in names[-2:]:require((Path('/sys/class/block')/name/'force_ro').read_text().strip()=='1','Boot-area force_ro missing')
    for unit in ['cron','udisks2','ChromaScreen','nginx']+[f'klipper-{i}' for i in range(1,9)]+[f'moonraker-{i}' for i in range(1,9)]:
        state=subprocess.run(['systemctl','show',unit+'.service','--property=UnitFileState','--value'],capture_output=True,text=True)
        require(state.stdout.strip() in ('masked','masked-runtime'),'Missing service mask: '+unit)
        state=subprocess.run(['systemctl','is-active',unit+'.service'],capture_output=True,text=True)
        require(state.stdout.strip()=='inactive','Recovery application running: '+unit)
    target_geometry(device)
    return device,names

def protected(device,fixture=False):
    values={str(a)+':'+str(z):region_hash(device,a,z-a) for a,z in RANGES}
    if not fixture:
        for suffix in ('boot0','boot1'):values[suffix]=region_hash(device+suffix,0,int(run('blockdev','--getsize64',device+suffix)))
    return values

def copy_region(device,start,length,path):
    with open(device,'rb',buffering=0) as src,Path(path).open('xb') as dst:
        src.seek(start)
        while length:
            data=src.read(min(CHUNK,length));require(bool(data),'Short FAT backup read');dst.write(data);length-=len(data)
        dst.flush();os.fsync(dst.fileno())

def boot_destination(fat,bundle,m):
    dest=fat/'debian'/m['boot_generation']
    if dest.exists():
        require(dest.is_dir() and not dest.is_symlink(),'Unsafe existing generation')
        for name,rec in m['boot_files'].items():
            p=dest/name;require(p.is_file() and p.stat().st_size==rec['bytes'] and sha(p)==rec['sha256'],'Existing generation incomplete or changed; inspect before retry')
        return dest,False
    need=sum(rec['bytes'] for rec in m['boot_files'].values())+16*1024**2
    require(shutil.disk_usage(fat).free>need,'Insufficient FAT space for new recovery generation')
    return dest,True

def transfer(bundle,m,device,work,manifest_sha,fixture=False):
    DISPLAY.update('Preparing the restore',3,detail='Saving boot files and checking protected firmware.')
    require(re.fullmatch(r'/dev/loop[0-9]+',device) if fixture else re.fullmatch(r'/dev/mmcblk[0-9]+',device),'Wrong target device class')
    work=Path(work);fat=work/'fat';fat.mkdir()
    mounts=[];rw=False;paused=False
    before=protected(device,fixture)
    if not fixture:require(before==m['protected_reference'],'Protected firmware differs from the verified backup reference')
    save(work/'protected-before.json',before)
    def mount(opts):command('mount','-t','vfat','-o',opts,device+'p1',fat);mounts.append(fat)
    def unmount():command('umount',fat);mounts.remove(fat)
    mount('ro,nosuid,nodev,noexec')
    try:
        dest,create=boot_destination(fat,bundle,m)
        entry=fat/'emmc_autoscript'
        require(not entry.exists() or entry.is_file(),'Existing boot entry is not a regular file')
        old_entry=sha(entry) if entry.exists() else None
    finally:unmount()
    require(shutil.disk_usage(work).free>PARTS[0][1]*512+256*1024**2,'Insufficient USB space for FAT backup and logs')
    copy_region(device,PARTS[0][0]*512,PARTS[0][1]*512,work/'boot-before.fat')
    save(work/'boot-before.json',{'sha256':sha(work/'boot-before.fat'),'entry_sha256':old_entry})
    try:
        if not fixture:
            device2,names=physical_target(m);require(device2==device,'Target changed before write')
            rule=Path('/run/udev/rules.d/99-chromapad-emmc-probe.rules').read_text()
            require('blockdev --setro /dev/%k' in rule and 'UDISKS_IGNORE' in rule,'Unexpected target protection rule')
            command('udevadm','settle','--timeout=30')
            paused=True;command('udevadm','control','--stop-exec-queue',timeout=30)
            rw=True
            for suffix in ('','p1','p2'):command('blockdev','--setrw',device+suffix)
        mount('rw,nosuid,nodev,noexec,umask=0077')
        dest,create=boot_destination(fat,bundle,m)
        if create:
            pending=fat/'debian'/('.staging-'+m['boot_generation'])
            require(not pending.exists(),'Previous interrupted boot staging requires review')
            pending.parent.mkdir(exist_ok=True);shutil.copytree(Path(bundle)/'boot',pending)
            for name,rec in m['boot_files'].items():require(sha(pending/name)==rec['sha256'],'Staged boot readback mismatch')
            command('sync','-f',fat);os.rename(pending,dest);command('sync','-f',fat)
        save(work/'phase.json',{'phase':'Boot generation staged; replacing only the existing p2 root','utc':utc()})
        report('Writing the backed-up filesystem only to '+device+'p2')
        fd=os.open(device+'p2',os.O_WRONLY|os.O_CLOEXEC|os.O_EXCL)
        try:
            stream_payload(bundle,m,fd)
            DISPLAY.update('Restoring Debian',4,detail='Finishing the write. Please keep power connected.')
            os.fsync(fd)
        finally:os.close(fd)
        command('blockdev','--flushbufs',device+'p2')
        report('Direct readback of the complete restored root partition')
        require(region_hash(device+'p2',0,RAW_BYTES,progress=True)==m['root_payload']['raw_sha256'],'Restored root readback mismatch')
        DISPLAY.update('Final checks',6,detail='Checking the filesystem, boot files and firmware.')
        with (work/'root-fsck.txt').open('w') as log:command('e2fsck','-f','-n',device+'p2',stdout=log,stderr=subprocess.STDOUT)
        require(run('blkid','-p','-c','/dev/null','-s','UUID','-o','value',device+'p2')==ROOT_UUID,'Restored root UUID mismatch')
        require(protected(device,fixture)==before,'Protected bytes changed before boot activation')
        previous=fat/('emmc_autoscript.before-recovery-'+work.name)
        require(not previous.exists(),'Boot entry backup already exists')
        if old_entry is not None:
            shutil.copyfile(fat/'emmc_autoscript',previous);require(sha(previous)==old_entry,'Previous boot entry readback mismatch')
        pending=fat/'.emmc_autoscript.recovery-new';require(not pending.exists(),'Previous interrupted entry activation requires review')
        with pending.open('xb') as f:f.write((dest/'emmc_autoscript').read_bytes());f.flush();os.fsync(f.fileno())
        command('sync','-f',fat);os.replace(pending,fat/'emmc_autoscript');command('sync','-f',fat)
        require(sha(fat/'emmc_autoscript')==m['boot_files']['emmc_autoscript']['sha256'],'Active boot entry readback mismatch')
        if fixture:require((fat/'preserve-existing.txt').read_text()=='Must survive recovery\n','Existing FAT content lost')
        unmount()
        with (work/'fat-fsck.txt').open('w') as log:command('fsck.vfat','-n',device+'p1',stdout=log,stderr=subprocess.STDOUT)
        after=protected(device,fixture);require(after==before,'Protected bytes changed during recovery')
        save(work/'protected-after.json',after)
    finally:
        errors=[]
        def cleanup(*a):
            try:command(*a,timeout=60)
            except Exception as exc:errors.append(str(exc))
        for p in reversed(mounts):cleanup('umount',p)
        if rw:
            for suffix in ('p1','p2',''):cleanup('blockdev','--setro',device+suffix)
            for suffix in ('','p1','p2','boot0','boot1'):
                try:require(run('blockdev','--getro',device+suffix)=='1','Read-only protection restore failed')
                except Exception as exc:errors.append(str(exc))
        if paused:cleanup('udevadm','control','--start-exec-queue')
        require(not errors,'Recovery cleanup requires attention: '+repr(errors))
    result={'status':'RESTORE_PASS','utc':utc(),'fixture':fixture,'manifest_sha256':manifest_sha,
            'root_partition_sha256':m['root_payload']['raw_sha256'],'protected_regions_unchanged':True,
            'filesystems_clean':True,'boot_entry_verified':True,'cleanup_complete':True,'reboot_performed':False}
    save(work/'result.json',result);return result

def fixture_disk(bundle,m,work):
    image=work/'disposable-target.img'
    with image.open('xb') as f:
        f.truncate(SIZE)
        data=(Path(bundle)/'target-mbr.bin').read_bytes();require(len(data)==512 and hashlib.sha256(data).hexdigest()==MBR_SHA,'Invalid fixture MBR')
        f.write(data)
        for offset in (37748736,1296039936,1312817152,1329594368):f.seek(offset);f.write((b'KEEP-RECOVERY-FIXTURE-'+str(offset).encode())*32)
    device=run('losetup','--find','--show','--partscan',image)
    try:
        require(run('losetup','-n','-O','BACK-FILE',device)==str(image),'Fixture loop backing mismatch')
        command('udevadm','settle','--timeout=30')
        command('mkfs.vfat','-F','32','-i',FAT_UUID.replace('-',''),device+'p1')
        fat=work/'fixture-fat';fat.mkdir();command('mount','-t','vfat',device+'p1',fat)
        try:
            (fat/'emmc_autoscript').write_bytes(b'Previous fixture entry\n')
            (fat/'preserve-existing.txt').write_text('Must survive recovery\n')
        finally:command('umount',fat)
    except BaseException:
        command('losetup','-d',device);raise
    return device

def main():
    global SCREEN,DISPLAY
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('bundle',type=Path);p.add_argument('--manifest-sha',required=True)
    mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--boot',action='store_true');mode.add_argument('--probe',action='store_true');mode.add_argument('--rehearse',action='store_true')
    a=p.parse_args();os.umask(0o077);require(os.geteuid()==0,'Root required')
    DISPLAY=RecoveryScreen(enabled=not a.rehearse)
    DISPLAY.update('Preparing recovery',detail='Checking the USB image and pad identity.')
    os.environ['PATH']='/usr/sbin:/usr/bin:/sbin:/bin:'+os.environ.get('PATH','')
    os.environ['SYSTEMD_IGNORE_CHROOT']='1';os.environ['SYSTEMD_OFFLINE']='0'
    def stop(signum,frame):raise RuntimeError('Interrupted by signal '+str(signum))
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,stop)
    m=validate_manifest(a.bundle,a.manifest_sha)
    if a.rehearse:
        require(a.bundle.resolve().is_relative_to(Path('/media/raid1/coprint_2_debian/experiments')),'Rehearsal requires private project stage')
        validate_payload(a.bundle,m)
        work=Path(tempfile.mkdtemp(prefix='engine-rehearsal-',dir=a.bundle.parent))
        device=fixture_disk(a.bundle,m,work)
        try:target_geometry(device,True);result=transfer(a.bundle,m,device,work,a.manifest_sha,True)
        finally:command('losetup','-d',device)
        print(json.dumps({'work':str(work),**result},indent=2));return
    require(a.bundle==Path('/payload'),'Physical recovery must use the read-only payload binding')
    require('ro' in run('findmnt','-n','-o','OPTIONS','--mountpoint','/payload').split(','),'Payload binding must be read-only')
    with open('/run/lock/chromapad-recovery.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        device,names=physical_target(m);SCREEN=True
        state=Path('/state')/m['build_id'];state.mkdir(mode=0o700,exist_ok=True)
        arm=Path('/usb-boot/recovery-arm.json')
        plan={'status':'READ_ONLY_PROBE_PASS','utc':utc(),'device':device,'cid':CID,'manifest_sha256':a.manifest_sha,
              'all_emmc_surfaces_readonly':True,'target_writes_performed':False,
              'confirmation_for_later_arming':arm_token(m,a.manifest_sha)}
        if a.probe or not arm.exists():
            validate_payload(a.bundle,m)
            require(protected(device)==m['protected_reference'],'Protected firmware differs from the verified backup reference')
            require(physical_target(m)==(device,names),'Target changed during probe')
            writes={name:int((Path('/sys/class/block')/name/'stat').read_text().split()[6]) for name in names}
            require(not any(writes.values()),'eMMC writes occurred during this USB boot')
            plan.update(payload_fully_verified=True,protected_firmware_verified=True,sectors_written_since_boot=writes)
            save(state/'probe.json',plan)
            report('READ-ONLY TEST PASSED. eMMC remains protected. USB is not armed for restoration.')
            DISPLAY.update('Checks passed',status='probe')
            print(json.dumps(plan,indent=2));return
        validate_arm(json.loads(arm.read_text()),m,a.manifest_sha)
        require(not (state/'attempt.json').exists() and not (state/'completed.json').exists(),'This USB already attempted restoration. Automatic repeat is blocked; inspect saved logs and deliberately prepare fresh media.')
        validate_payload(a.bundle,m)
        device2,names2=physical_target(m);require((device2,names2)==(device,names),'Target changed')
        work=Path(tempfile.mkdtemp(prefix='restore-',dir=state))
        save(state/'attempt.json',{'utc':utc(),'manifest_sha256':a.manifest_sha,'run':work.name,'state':'STARTED_NO_AUTOMATIC_RETRY'},exclusive=True)
        command('sync','-f',state)
        try:
            result=transfer(a.bundle,m,device,work,a.manifest_sha)
            save(state/'completed.json',result,exclusive=True);command('sync','-f',state)
        except BaseException as exc:
            try:save(work/'failure.json',{'error':str(exc),'utc':utc(),'automatic_retry_blocked':True})
            except Exception:pass
            report('RESTORE FAILED. Do not assume the root is usable. Automatic retry is blocked. '+str(exc));raise
        report('RESTORE COMPLETE. eMMC is protected read-only again. Shut down, remove USB, then cold-start the pad.')
        DISPLAY.update('Recovery complete',status='complete')
        # Stay available for log inspection; neither reboot nor poweroff is automatic.

if __name__=='__main__':
    try:main()
    except BaseException as exc:
        if not isinstance(exc,SystemExit) or exc.code:
            DISPLAY.update('Recovery stopped',status='failed',detail=str(exc))
        raise
    finally:DISPLAY.finish()

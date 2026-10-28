"""Portable recovery builder helpers: native on the pad, emulated in PC tests."""
import contextlib, json, os, platform, signal, subprocess, sys
from pathlib import Path
import chromapad_debian_backup as b

LOCAL=Path(__file__).resolve().parent
BASE=Path('/media/raid1/coprint_2_debian')
TOOLS=Path(os.environ.get('CHROMAPAD_BUILD_TOOLS','/var/tmp/chromapad-build-tools-6l5r2nhw/extracted'))
LEGACY_USB_PARTS=[(2048,1048576),(1050624,25165824)]
USB_PARTS=[(2048,1048576),(1050624,50331648)]
USB_BYTES=sum(USB_PARTS[1])*512

def configure_layout(image_bytes):
    """Select an owned image's capacity without relaxing its fixed boot layout."""
    global USB_PARTS,USB_BYTES
    root_bytes=image_bytes-1050624*512
    b.require(type(image_bytes) is int and 1024**3<=root_bytes<=128*1024**3
              and root_bytes%(512*1024**2)==0,'Unsupported recovery image capacity')
    USB_PARTS=[(2048,1048576),(1050624,root_bytes//512)]
    USB_BYTES=image_bytes

NATIVE=platform.machine() in ('aarch64','arm64')

def install_signal_handlers():
    def interrupted(signum,frame):raise RuntimeError('Build interrupted by signal '+str(signum))
    for number in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(number,interrupted)

def private():
    if '--private' not in sys.argv:
        os.execvp('unshare',['unshare','--mount','--propagation','private',sys.executable,sys.argv[0],*sys.argv[1:],'--private'])
    os.environ['PATH']='/usr/sbin:/usr/bin:/sbin:/bin'
    if not NATIVE and TOOLS.exists():
        os.environ['PATH']+=':'+str(TOOLS/'usr/sbin')+':'+str(TOOLS/'usr/bin')
        os.environ['LD_LIBRARY_PATH']=str(TOOLS/'usr/lib/x86_64-linux-gnu')
    os.umask(0o077)
    install_signal_handlers()

@contextlib.contextmanager
def project():
    b.require(BASE.is_dir(),'NAS project unavailable')
    meta=json.loads((LOCAL/'recovery-usb-stage.json').read_text())
    stage=Path(meta['stage'])
    b.require(stage.parent==BASE/'experiments' and stage.name.startswith('debian-recovery-') and not stage.is_symlink(),'Unsafe stage')
    b.require(stage.resolve().parent==BASE.resolve()/'experiments','Stage escaped project')
    yield stage,meta

@contextlib.contextmanager
def usb_image(stage,writable=False,image=None):
    image=Path(image) if image is not None else stage/'recovery-usb.img'
    b.require(image.is_file() and not image.is_symlink() and image.stat().st_size==USB_BYTES,'Invalid USB image')
    mounts=[];loops=[]
    try:
        points=[]
        for i,(start,sectors) in enumerate(USB_PARTS):
            args=['losetup','--find','--show','--offset',start*512,'--sizelimit',sectors*512]
            if not writable:args.append('--read-only')
            loop=b.run(*args,image);loops.append(loop)
            b.require(b.run('losetup','-n','-O','BACK-FILE',loop)==str(image),'Unexpected loop')
            point=stage/('usb-fat' if i==0 else 'usb-root');point.mkdir(exist_ok=True)
            opts='rw,noatime,nosuid,nodev' if writable else 'ro,nosuid,nodev,noexec'+(',noload' if i else '')
            b.command('mount','-t','vfat' if i==0 else 'ext4','-o',opts,loop,point);mounts.append((loop,point));points.append(point)
        yield points
    finally:
        b.cleanup_image(image,loops,mounts)

@contextlib.contextmanager
def emulator():
    if NATIVE:
        yield;return
    fmt=Path('/proc/sys/fs/binfmt_misc')
    if not (fmt/'register').exists():b.command('mount','-t','binfmt_misc','binfmt_misc',fmt)
    handler=fmt/'chromapad-recovery-aarch64'
    b.require(not handler.exists(),'Temporary emulator already registered')
    entry=(TOOLS/'usr/lib/binfmt.d/qemu-aarch64.conf').read_text().strip().replace('\\\\','\\')
    entry=entry.replace(':qemu-aarch64:',':chromapad-recovery-aarch64:').replace('/usr/bin/qemu-aarch64',str(TOOLS/'usr/bin/qemu-aarch64'))
    (fmt/'register').write_text(entry+'\n')
    try:yield
    finally:
        if handler.exists():handler.write_text('-1\n')

@contextlib.contextmanager
def minimal_devices(root):
    point=root/'dev';point.mkdir(exist_ok=True)
    b.command('mount','-t','tmpfs','-o','mode=755,size=1M','tmpfs',point)
    try:
        for name,major,minor in [('null',1,3),('zero',1,5),('random',1,8),('urandom',1,9)]:b.command('mknod','-m','666',point/name,'c',major,minor)
        yield
    finally:b.command('umount',point)

def arm(root,*args,**kwargs):
    return b.run('chroot',root,'/usr/bin/env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin','HOME=/root','LANG=C.UTF-8','PYTHONDONTWRITEBYTECODE=1',*args,**kwargs)

@contextlib.contextmanager
def scratch(root):
    point=root/'var/tmp/recovery-build';point.mkdir()
    b.command('mount','-t','tmpfs','-o','size=1G,mode=700,nosuid','tmpfs',point)
    try:yield point
    finally:b.command('umount',point);point.rmdir()

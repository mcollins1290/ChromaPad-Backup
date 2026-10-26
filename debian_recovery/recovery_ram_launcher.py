#!/usr/bin/python3
"""Validate and enter a private RAM toolchain before accessing a restore target."""
import hashlib,json,os,subprocess,sys,tarfile
from pathlib import Path

def require(ok,message):
    if not ok:raise RuntimeError(message)
def cmd(*args):subprocess.run([str(x) for x in args],check=True,timeout=60)
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while data:=f.read(4*1024*1024):h.update(data)
    return h.hexdigest()

if '--private' not in sys.argv:
    os.execv('/usr/bin/unshare',['unshare','--mount','--propagation','private','/usr/bin/python3',__file__,*sys.argv[1:],'--private'])
sys.path.insert(0,'/usr/local/lib/chromapad-recovery')
from recovery_ui import RecoveryScreen
screen=RecoveryScreen()
screen.update('Preparing recovery',detail='Loading and checking the recovery tools.')
try:
    require(os.geteuid()==0,'Root required')
    os.umask(0o077)
    bundle=Path('/opt/chromapad-recovery')
    release=json.loads((bundle/'release.json').read_text())
    expected=release['manifest_sha256']
    require(sha(bundle/'manifest.json')==expected,'Manifest differs from release pin')
    m=json.loads((bundle/'manifest.json').read_text())
    require(m['runtime']['file']=='ram-tools.tar.gz','Unexpected runtime filename')
    archive=bundle/m['runtime']['file']
    require(sha(archive)==m['runtime']['sha256'],'RAM tool archive checksum failed')
    root=Path('/run/chromapad-recovery-ram')
    root.mkdir(exist_ok=True)
    require(not root.is_mount() and not any(root.iterdir()),'RAM mountpoint already in use')
    cmd('mount','-t','tmpfs','-o','mode=0700,size=512M,nosuid','chromapad-recovery-ram',root)
    with tarfile.open(archive,'r:gz') as tar:tar.extractall(root,filter='data')
    require(sha(root/'recovery/chromapad_recovery.py')==m['engine_sha256'],'RAM engine checksum failed')
    def bind(source,target,readonly=False):
        point=root/target;point.mkdir(parents=True,exist_ok=True)
        cmd('mount','--bind',source,point)
        if readonly:cmd('mount','-o','remount,bind,ro',point)
    bind('/dev','dev')
    (root/'proc').mkdir(exist_ok=True);cmd('mount','-t','proc','proc',root/'proc')
    bind('/sys','sys',True)
    bind('/run','run')
    bind('/','host/root',True)
    bind('/boot/recovery','usb-boot',True)
    bind(bundle,'payload',True)
    state=Path('/var/lib/chromapad-recovery');state.mkdir(mode=0o700,exist_ok=True)
    bind(state,'state')
    os.chroot(root);os.chdir('/')
    env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','LANG':'C.UTF-8','SYSTEMD_IGNORE_CHROOT':'1','SYSTEMD_OFFLINE':'0','PYTHONDONTWRITEBYTECODE':'1'}
    screen.finish()
    os.execve('/usr/bin/python3',['python3','/recovery/chromapad_recovery.py','/payload','--manifest-sha',expected,'--boot'],env)
    
except BaseException as exc:
    screen.update('Recovery stopped',status='failed',detail=str(exc))
    screen.finish()
    raise
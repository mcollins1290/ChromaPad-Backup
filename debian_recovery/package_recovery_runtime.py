"""Build a minimal ARM RAM toolchain from the verified offline Debian root."""
import os,re,shutil,stat,subprocess,tarfile
from pathlib import Path

def package(root,out,engine):
    root=Path(root);out=Path(out);tree=out/'ram-tools'
    if tree.exists():raise RuntimeError('RAM runtime tree already exists')
    tree.mkdir();copied=set();elfs=[]
    def add(relative):
        relative=Path(str(relative).lstrip('/'))
        if '..' in relative.parts:raise RuntimeError('Unsafe runtime path')
        if relative.parts[0] in ('lib','bin','sbin'):relative=Path('usr')/relative
        if str(relative) in copied:return
        copied.add(str(relative))
        src=root/relative;dst=tree/relative
        dst.parent.mkdir(parents=True,exist_ok=True)
        if src.is_symlink():
            link=os.readlink(src)
            if link.startswith('/'):
                target=Path(link.lstrip('/'))
                link=os.path.relpath(tree/target,dst.parent)
            else:target=Path(os.path.normpath(str(relative.parent/link)))
            dst.symlink_to(link);add(target);return
        if not src.is_file():raise RuntimeError('Runtime dependency missing: '+str(src))
        shutil.copy2(src,dst)
        with src.open('rb') as f:
            if f.read(4)==b'\x7fELF':elfs.append(src)
    for name,target in [('bin','usr/bin'),('sbin','usr/sbin'),('lib','usr/lib')]:
        (tree/name).symlink_to(target)
    (tree/'usr/lib').mkdir(parents=True,exist_ok=True)
    # Shared-library names in /lib are canonicalized to /usr/lib to avoid duplicate copies.
    def canonical(name):return 'usr/'+name if name.startswith(('lib/','bin/','sbin/')) else name
    for name in ('python3','mount','umount','findmnt','udevadm','systemctl','sync','true','env'):
        add('usr/bin/'+name)
    for name in ('blkid','blockdev','e2fsck','fsck.vfat'):add('usr/sbin/'+name)
    # Render in RAM, independently of application services and USB reads.
    for p in (root/'usr/lib/python3/dist-packages/PIL').rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:add(str(p.relative_to(root)))
    add('usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    stdlib=root/'usr/lib/python3.13'
    for p in stdlib.rglob('*'):
        rel=p.relative_to(stdlib)
        if any(part in ('__pycache__','test','tests','idlelib','tkinter','turtledemo','ensurepip') for part in rel.parts):continue
        if p.is_file() or p.is_symlink():add(str(p.relative_to(root)))
    index=0
    while index<len(elfs):
        binary=elfs[index];index+=1
        data=subprocess.check_output(['readelf','-l','-d',binary],text=True)
        interp=re.search(r'Requesting program interpreter: ([^\]]+)',data)
        if interp:add(canonical(interp[1].lstrip('/')))
        for name in re.findall(r'Shared library: \[([^\]]+)\]',data):
            candidates=[root/'usr/lib/aarch64-linux-gnu'/name,root/'usr/lib'/name,root/'usr/lib/systemd'/name,root/'usr/lib/aarch64-linux-gnu/systemd'/name]
            found=next((p for p in candidates if p.exists()),None)
            if found is None:raise RuntimeError('Missing '+name+' for '+str(binary))
            add(str(found.relative_to(root)))
    for name in ('passwd','group','nsswitch.conf','hosts'):add('etc/'+name)
    for name in ('tmp','run','root','dev','proc','sys','state','payload','usb-boot','host/root','recovery'):(tree/name).mkdir(parents=True,exist_ok=True)
    (tree/'tmp').chmod(0o1777)
    shutil.copy2(engine,tree/'recovery/chromapad_recovery.py')
    shutil.copy2(Path(engine).with_name('recovery_ui.py'),tree/'recovery/recovery_ui.py')
    (tree/'recovery/runtime.marker').write_text('ChromaPad recovery RAM tools v1\n')
    size=sum(p.stat().st_size for p in tree.rglob('*') if p.is_file() and not p.is_symlink())
    if size>350*1024**2:raise RuntimeError('RAM runtime too large: '+str(size))
    with tarfile.open(out/'ram-tools.tar.gz','w:gz',compresslevel=1) as tar:
        for p in sorted(tree.iterdir()):tar.add(p,arcname=p.name)
    return {'uncompressed_file_bytes':size,'elf_files':len(elfs),'files':len(copied)}

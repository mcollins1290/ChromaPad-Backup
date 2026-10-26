#!/usr/bin/python3
"""Run only inside the disposable offline ARM clone, with no block devices."""
import hashlib,json,os,shutil,stat,subprocess
from pathlib import Path
work=Path('/var/tmp/recovery-build');tree=work/'initrd-tree'
def cmd(*a,**kw):subprocess.run([str(x) for x in a],check=True,**kw)
def inventory(root):
    result={}
    for p in root.rglob('*'):
        info=p.lstat();rec={'mode':info.st_mode,'uid':info.st_uid,'gid':info.st_gid}
        if p.is_symlink():rec['link']=os.readlink(p)
        elif p.is_file():rec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
        elif not p.is_dir():rec['rdev']=info.st_rdev
        result[str(p.relative_to(root))]=rec
    return result
cmd('unmkinitramfs',work/'initrd.raw',tree)
if (tree/'init').is_file():main=tree;parts=[tree]
else:
    main=tree/'main'
    assert (main/'init').is_file(),'Unknown initramfs layout'
    parts=sorted((p for p in tree.iterdir() if p.name.startswith('early')),key=lambda p:(len(p.name),p.name))+[main]
    assert set(parts)==set(tree.iterdir()),'Unknown archive part'
before={p.name:inventory(p) for p in parts}
init=(main/'init').read_text()
for mode,anchor in [('protect','run_scripts /scripts/init-top\n'),('verify','run_scripts /scripts/init-bottom\n')]:
    assert init.count(anchor)==1
    barrier=('if ! /scripts/chromapad-emmc-guard '+mode+'; then\n'
             ' echo "ChromaPad recovery: eMMC protection failed; boot stopped." > /dev/console\n'
             ' while :; do sleep 60; done\nfi\n')
    init=init.replace(anchor,barrier+anchor if mode=='protect' else anchor+barrier)
(main/'init').write_text(init)
shutil.copyfile(work/'recovery-emmc-guard.sh',main/'scripts/chromapad-emmc-guard')
(main/'scripts/chromapad-emmc-guard').chmod(0o755)
for part in ('usr/bin','usr/sbin','usr/lib','lib','bin','sbin'):assert (main/part).resolve().is_relative_to(main)
env={**os.environ,'DESTDIR':str(main),'verbose':'n'}
cmd('/bin/bash','-eu','-c','. /usr/share/initramfs-tools/hook-functions\ncopy_exec /usr/sbin/blockdev\ncopy_exec /usr/sbin/blkid\ncopy_exec /usr/bin/sha256sum\n',env=env)
for script in ('init','scripts/chromapad-emmc-guard'):cmd('/bin/sh','-n',main/script)
checks={}
for binary in ('/usr/sbin/blockdev','/usr/sbin/blkid','/usr/bin/sha256sum'):
    checks[binary]=subprocess.check_output(['chroot',str(main),binary,'--version'],text=True)
after={p.name:inventory(p) for p in parts}
changes={}
for p in parts:
    old=before[p.name];new=after[p.name]
    assert not set(old)-set(new),'Initramfs entry removed'
    changed=[k for k in old if old[k]!=new[k]]
    assert not changed or (p==main and changed==['init']),changed
    changes[p.name]={'changed':changed,'added':sorted(set(new)-set(old))}
with (work/'guarded-initrd.raw').open('wb') as joined:
    for i,p in enumerate(parts):
        archive=work/('part-'+str(i)+'.cpio')
        paths=b'\0'.join(os.fsencode(str(x.relative_to(p))) for x in [p,*sorted(p.rglob('*'))])+b'\0'
        with archive.open('wb') as f:cmd('cpio','--null','-o','--format=newc','--quiet','--owner=0:0',cwd=p,input=paths,stdout=f)
        if p==main:cmd('gzip','-n','-1','-c',archive,stdout=joined)
        else:
            with archive.open('rb') as src:shutil.copyfileobj(src,joined)
verify=work/'verify-tree';cmd('unmkinitramfs',work/'guarded-initrd.raw',verify)
if main==tree:assert inventory(verify)==after[main.name]
else:
    for p in parts:assert inventory(verify/p.name)==after[p.name],p.name
assert init.index('/scripts/chromapad-emmc-guard protect')<init.index('run_scripts /scripts/init-top')
assert init.index('/scripts/chromapad-emmc-guard verify')>init.index('run_scripts /scripts/init-bottom')
(work/'initramfs-report.json').write_text(json.dumps({'status':'PASS','archive_parts':[p.name for p in parts],'changes':changes,'repack_inventory_identical':True,'arm_tools':checks},indent=2)+'\n')
print('Guarded initramfs built and verified',flush=True)

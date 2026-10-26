"""Exercise the actual ARM launcher in an offline clone with NO block devices.

Success is entering the RAM engine and refusing the non-USB host before target
operations. All mounts are confined to private mount namespaces.
"""
import json,subprocess
import recovery_build_common as c
from recovery_build_common import b
def smoke(stage,m,image=None):
 with c.usb_image(stage,image=image) as (fat,root):
    b.command('mount','-o','remount,ro,exec,nosuid,nodev',root)
    mounts=[]
    try:
        for name in ('run','var/lib/chromapad-recovery'):
            b.command('mount','-t','tmpfs','-o','mode=755,size=16M','tmpfs',root/name);mounts.append(root/name)
        (root/'run/lock').mkdir()
        b.command('mount','-t','proc','proc',root/'proc');mounts.append(root/'proc')
        for source,dest in [('/sys',root/'sys'),(fat,root/'boot/recovery')]:
            b.command('mount','--bind',source,dest);mounts.append(dest)
            b.command('mount','-o','remount,bind,ro',dest)
        with c.emulator(),c.minimal_devices(root):
            result=subprocess.run(['chroot',str(root),'/usr/bin/env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin','PYTHONDONTWRITEBYTECODE=1','/usr/bin/python3','/usr/local/sbin/chromapad-recovery-launcher'],capture_output=True,text=True,timeout=180)
        (stage/'reports/launcher-smoke.txt').write_text(result.stdout+'\n'+result.stderr)
        b.require(result.returncode!=0 and '/recovery/chromapad_recovery.py' in result.stderr and ('This is not the recovery USB root' in result.stderr or 'Recovery root is not a USB partition' in result.stderr),'Launcher did not reach the expected RAM engine safety refusal; inspect report')
        result={'status':'PASS','actual_arm_launcher_executed':True,'ram_engine_reached':True,'non_usb_host_refused':True,'block_devices_exposed_to_test':False,'physical_pad_accessed':False}
        b.record(stage/'reports/launcher-smoke.json',result);print(json.dumps(result,indent=2))
    finally:
        for p in reversed(mounts):b.command('umount',p)


if __name__=='__main__':
    c.private()
    with c.project() as (stage,m):smoke(stage,m)

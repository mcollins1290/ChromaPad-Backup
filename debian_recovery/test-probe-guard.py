#!/usr/bin/env python3
"""Exercise the shell guard using regular files and mocked tools, never devices."""
import base64,json,os,subprocess,tempfile
from pathlib import Path

base=Path(__file__).resolve().parent
source=(base/'probe-emmc-guard.sh').read_text()
evidence=json.loads((base/'emmc-readiness-evidence.json').read_text(encoding='utf-8-sig'))
mbr=base64.b64decode(evidence['saved_mbr_base64'])
cases=[]
scenarios=('success','wrong_root','missing_probe_token','recovery_token','missing_device',
           'wrong_name','wrong_serial','wrong_capacity','wrong_sector_size','wrong_mbr',
           'wrong_partition','missing_boot1','extra_disk','setro_failure','module_failure',
           'force_ro_off','late_writable','late_cid_change','late_wrong_root','late_mmc_mount')
for scenario in scenarios:
    with tempfile.TemporaryDirectory(prefix='emmc-guard-test-') as tmp:
        root=Path(tmp)
        for name in ('bin','dev','sys/block','sys/class/block','proc','run/initramfs','ro'):
            (root/name).mkdir(parents=True,exist_ok=True)
        def put(path,value):
            p=root/path;p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(str(value)+'\n')
        cmd='root=UUID=38327d56-529b-498f-8b1a-e81b534bfeb9 chromapad.emmc_probe=readonly'
        if scenario=='wrong_root':cmd=cmd.replace('38327d56-529b-498f-8b1a-e81b534bfeb9','wrong')
        if scenario=='missing_probe_token':cmd=cmd.split()[0]
        if scenario=='recovery_token':cmd+=' autoflash=301'
        put('proc/cmdline',cmd)
        put('proc/mounts',f'{root}/dev/sda2 /root ext4 rw 0 0')
        put('dev/sda2','USB')
        put('dev/kmsg','')
        surfaces=['mmcblk0','mmcblk0p1','mmcblk0p2','mmcblk0boot0','mmcblk0boot1']
        if scenario=='missing_device':surfaces=[]
        if scenario=='missing_boot1':surfaces.remove('mmcblk0boot1')
        if scenario=='extra_disk':surfaces.append('mmcblk1')
        for name in surfaces:
            put(f'sys/class/block/{name}/dev','179:0')
            (root/'dev'/name).write_bytes(mbr if name=='mmcblk0' else b'fixture')
            put(f'ro/{name}','0')
            if name in ('mmcblk0','mmcblk1') or 'boot' in name:
                (root/'sys/block'/name).symlink_to(root/'sys/class/block'/name)
        if 'mmcblk0' in surfaces:
            for key,val in {'type':'MMC','name':'AJNB4R','serial':'0x35a5fac6','cid':'1'*32}.items():
                put(f'sys/class/block/mmcblk0/device/{key}',val)
            for p,start,size in ((1,139264,1046528),(2,3289088,27246592)):
                put(f'sys/class/block/mmcblk0p{p}/start',start)
                put(f'sys/class/block/mmcblk0p{p}/size',size)
            for p in (0,1):put(f'sys/class/block/mmcblk0boot{p}/force_ro',1)
        if scenario=='extra_disk':put('sys/class/block/mmcblk1/device/type','SD')
        if scenario=='wrong_name':put('sys/class/block/mmcblk0/device/name','different')
        if scenario=='wrong_serial':put('sys/class/block/mmcblk0/device/serial','0x12345678')
        if scenario=='wrong_partition':put('sys/class/block/mmcblk0p2/start','42')
        if scenario=='wrong_mbr':(root/'dev/mmcblk0').write_bytes(bytes(512))
        if scenario=='force_ro_off':put('sys/class/block/mmcblk0boot0/force_ro',0)
        mock='''#!/usr/bin/python3
import os,sys
from pathlib import Path
r=Path(os.environ['FIXTURE']); scenario=os.environ['SCENARIO']; name=Path(sys.argv[0]).name
if name=='sleep':sys.exit(0)
if name=='modprobe':sys.exit(1 if scenario=='module_failure' else 0)
if name=='blkid':
 print('wrong' if os.environ.get('LATE_WRONG_ROOT') else '38327d56-529b-498f-8b1a-e81b534bfeb9');sys.exit(0)
arg,dev=sys.argv[1:]; p=r/'ro'/Path(dev).name
if arg=='--setro':
 if scenario=='setro_failure' and dev.endswith('p2'):sys.exit(1)
 p.write_text('1\\n')
elif arg=='--getro':print(p.read_text().strip())
elif arg=='--getsize64':print(42 if scenario=='wrong_capacity' else 15634268160)
elif arg=='--getss':print(4096 if scenario=='wrong_sector_size' else 512)
else:sys.exit(99)
'''
        for tool in ('blockdev','modprobe','sleep','blkid'):
            p=root/'bin'/tool;p.write_text(mock);p.chmod(0o755)
        test_source=source
        for path in ('/sys/','/dev/','/proc/','/run/'):
            test_source=test_source.replace(path,str(root)+path)
        test_source=test_source.replace('PATH=/usr/sbin:/usr/bin:/sbin:/bin',f'PATH={root}/bin:/usr/bin:/bin')
        test_source=test_source.replace('[ -b ', '[ -f ')
        script=root/'guard';script.write_text(test_source)
        env={**os.environ,'FIXTURE':str(root),'SCENARIO':scenario}
        first=subprocess.run(['/bin/sh',str(script),'protect'],env=env,capture_output=True,text=True,timeout=10)
        expected_first=scenario=='success' or scenario.startswith('late_')
        assert (first.returncode==0)==expected_first,(scenario,first.returncode,first.stderr,(root/'run/initramfs/chromapad-emmc-probe.log').read_text())
        if expected_first:
            if scenario=='late_writable':put('ro/mmcblk0p2',0)
            if scenario=='late_cid_change':put('sys/class/block/mmcblk0/device/cid','2'*32)
            if scenario=='late_wrong_root':env['LATE_WRONG_ROOT']='1'
            if scenario=='late_mmc_mount':
                # The fixture-translated script checks its own device namespace.
                put('proc/mounts',f'{root}/dev/mmcblk0p2 /root ext4 ro 0 0')
            last=subprocess.run(['/bin/sh',str(script),'verify'],env=env,capture_output=True,text=True,timeout=10)
            assert (last.returncode==0)==(scenario=='success'),(scenario,last.returncode,last.stderr)
        cases.append({'scenario':scenario,'result':'PASS'})
(base/'probe-guard-tests.json').write_text(json.dumps({'model':'regular-file fixtures and mocked tools; no real devices or boot tested','cases':cases},indent=2)+'\n')
print(f'{len(cases)} guard scenarios passed; no devices accessed.')

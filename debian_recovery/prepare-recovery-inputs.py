"""Extract current boot inputs from a sealed backup into a private build."""
import json, shutil
from pathlib import Path
import recovery_build_common as c
from recovery_build_common import b
c.private()
with c.project() as (stage,m):
    source=Path(m['backup_image']);manifest=json.loads((source.parent/'manifest.json').read_text())
    b.require(manifest['kind']=='chromapad-debian-emmc-backup' and manifest['source']['cid']==b.CID,'Wrong backup')
    b.require(b.digest(source)==m['backup_sha256'],'Backup checksum mismatch')
    with b.mounted_image(source,stage/'inputs') as (fat,root):
        shutil.copytree(root/'usr/lib/chromapad/emmc',stage/'inputs/helpers')
        boot=b.validate_boot(fat,root)
        m['kernel']=json.loads((root/'usr/lib/chromapad/emmc/identifiers.json').read_text())['kernel']
        policy=root/'etc/tmpfiles.d/chromapad-nginx-logs.conf'
        b.require(policy.is_file() and 'd /var/log/nginx 0755 root adm -' in policy.read_text(),'Backup lacks nginx log-directory recovery policy')
        b.require((root/'usr/lib/python3/dist-packages/PIL/Image.py').is_file(),'Backup lacks recovery display library')
        shutil.copytree(fat/'debian'/boot['generation'],stage/'inputs/emmc-boot')
        shutil.copyfile(root/'boot'/('initrd.img-'+m['kernel']),stage/'inputs/initrd.raw')
    for name in ('usb-prefix.bin','usb-boot.cmd'):
        shutil.copyfile(c.LOCAL/'assets'/name,stage/'inputs'/name)
    prefix=(stage/'inputs/usb-prefix.bin').read_bytes()
    b.require(len(prefix)==1048576 and prefix[510:512]==b'\x55\xaa','Invalid pinned USB prefix')
    b.record(stage/'stage.json',m);b.record(c.LOCAL/'recovery-usb-stage.json',m)
    b.record(stage/'reports/inputs.json',{'status':'PASS','backup_sha256':m['backup_sha256'],'nginx_recovery_policy_present':True})

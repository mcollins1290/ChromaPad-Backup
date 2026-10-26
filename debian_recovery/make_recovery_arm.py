#!/usr/bin/python3
"""Create a separate arming JSON only after a matching physical read-only probe.

Never opens a block device or changes a recovery image. No output without an
explicit confirmation token. Copying the result to USB is a separate action.
"""
import argparse,hashlib,json,os,re
from pathlib import Path
CID='150100414a4e4234520735a5fac66300'
def require(ok,message):
    if not ok:raise RuntimeError(message)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest',type=Path);p.add_argument('--manifest-sha',required=True)
    p.add_argument('--probe-result',type=Path);p.add_argument('--confirm');p.add_argument('--output',type=Path)
    a=p.parse_args();raw=a.manifest.read_bytes();require(hashlib.sha256(raw).hexdigest()==a.manifest_sha,'Manifest checksum mismatch')
    m=json.loads(raw);require(m['target']['cid']==CID and m['kind']=='chromapad-usb-recovery','Wrong target')
    token='RESTORE-DEBIAN:'+CID+':'+m['build_id']+':'+a.manifest_sha
    if a.confirm is None:
        require(a.output is None,'Output requires confirmation')
        print(json.dumps({'mode':'PLAN_ONLY','required_confirmation':token,'required_probe':'Physical READ_ONLY_PROBE_PASS for this manifest'},indent=2));return
    require(a.confirm==token and a.output is not None and a.probe_result is not None,'Exact confirmation, probe result and new output filename required')
    probe=json.loads(a.probe_result.read_text())
    require(probe['status']=='READ_ONLY_PROBE_PASS' and probe['cid']==CID and probe['manifest_sha256']==a.manifest_sha,'Probe is for a different pad or release')
    for flag in ('all_emmc_surfaces_readonly','payload_fully_verified','protected_firmware_verified'):require(probe.get(flag) is True,'Incomplete physical probe')
    require(probe.get('target_writes_performed') is False and len(probe['sectors_written_since_boot'])==5 and not any(probe['sectors_written_since_boot'].values()),'Probe did not establish zero eMMC writes')
    require(a.output.name=='recovery-arm.json' and not a.output.exists() and not a.output.is_symlink(),'Output must be a new recovery-arm.json file')
    require(not str(a.output.resolve()).startswith('/dev/'),'Device output forbidden')
    value={'schema':1,'action':'restore-emmc','cid':CID,'build_id':m['build_id'],'manifest_sha256':a.manifest_sha,'confirmation':token}
    os.umask(0o077)
    with a.output.open('x') as f:json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    print('Created separate arming file. No image or physical device was modified.')
if __name__=='__main__':main()

import json,subprocess,tempfile
from pathlib import Path
import recovery_build_common as c
from recovery_build_common import b
import chromapad_recovery as r
c.private()
with c.project() as (stage,m):
    pin=json.loads((stage/'payload/release.json').read_text())['manifest_sha256']
    manifest=json.loads((stage/'payload/manifest.json').read_text())
    cli=['python3',str(c.LOCAL/'make_recovery_arm.py'),str(stage/'payload/manifest.json'),'--manifest-sha',pin]
    plan=json.loads(b.run(*cli));b.require(plan['mode']=='PLAN_ONLY','CLI default is not plan-only')
    with tempfile.TemporaryDirectory(prefix='recovery-arming-cli-') as tmp:
        folder=Path(tmp);output=folder/'recovery-arm.json';probe=folder/'probe.json'
        good={'status':'READ_ONLY_PROBE_PASS','cid':r.CID,'manifest_sha256':pin,'all_emmc_surfaces_readonly':True,'payload_fully_verified':True,'protected_firmware_verified':True,'target_writes_performed':False,'sectors_written_since_boot':{name:0 for name in ('mmcblk1','mmcblk1p1','mmcblk1p2','mmcblk1boot0','mmcblk1boot1')}}
        # Synthetic fixtures test the CLI contract only, never a physical result.
        bad={**good,'payload_fully_verified':False};probe.write_text(json.dumps(bad))
        args=cli+['--probe-result',str(probe),'--confirm',plan['required_confirmation'],'--output',str(output)]
        refused=subprocess.run(args,capture_output=True,text=True)
        b.require(refused.returncode!=0 and not output.exists(),'Incomplete probe was accepted')
        probe.write_text(json.dumps(good));b.run(*args)
        r.validate_arm(json.loads(output.read_text()),manifest,pin)
        repeated=subprocess.run(args,capture_output=True,text=True)
        b.require(repeated.returncode!=0,'Arming output was overwritten')
    result={'status':'PASS','default_plan_only':True,'incomplete_probe_rejected':True,'exact_fixture_contract_passed':True,'overwrite_rejected':True,'synthetic_arming_output_removed':True,'physical_arming_performed':False}
    b.record(stage/'reports/arming-cli-tests.json',result);print(json.dumps(result,indent=2))

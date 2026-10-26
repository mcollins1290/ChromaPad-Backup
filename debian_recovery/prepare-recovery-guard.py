import json,subprocess
from pathlib import Path
base=Path(__file__).resolve().parent
m=json.loads((base/'recovery-usb-stage.json').read_text())
old='38327d56-529b-498f-8b1a-e81b534bfeb9'
guard=(base/'probe-emmc-guard.sh').read_text().replace(old,m['usb_root_uuid'])
guard=guard.replace('probe_cid=$(cat "$probe_device/cid")','probe_cid=$(cat "$probe_device/cid")\n[ "$probe_cid" = '+m['cid']+' ] || fail "Target CID mismatch"')
(base/'recovery-emmc-guard.sh').write_text(guard)
test=(base/'test-probe-guard.py').read_text().replace('probe-emmc-guard.sh','recovery-emmc-guard.sh').replace(old,m['usb_root_uuid']).replace("'cid':'1'*32","'cid':'"+m['cid']+"'").replace('probe-guard-tests.json','recovery-guard-tests.json')
test=test.replace("'wrong_name','wrong_serial'","'wrong_cid','wrong_name','wrong_serial'")
test=test.replace("        if scenario=='wrong_name':", "        if scenario=='wrong_cid':put('sys/class/block/mmcblk0/device/cid','1'*32)\n        if scenario=='wrong_name':")
(base/'test-recovery-guard.py').write_text(test)
subprocess.run(['python3',str(base/'test-recovery-guard.py')],check=True)

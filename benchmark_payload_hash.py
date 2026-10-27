#!/usr/bin/python3
"""Read-only comparison of sequential and sparse-aware payload hashing.

Run with sudo on the configured pad. No backup or printer services are started.
Results are saved to /var/tmp/chromapad-payload-hash-benchmark.json.
"""
import fcntl,json,os,shutil,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'debian_recovery'))
import rolling_recovery as rolling
import recovery_build_common as c
import rolling_backup as controller
controller.require(os.geteuid()==0, 'Run with sudo')
controller.require(controller.DEST.is_dir(), 'NAS directory unavailable')
c.private()
image=controller.IMAGE
stage=Path(tempfile.mkdtemp(prefix='chromapad-hash-benchmark-'))
(stage/'reports').mkdir()
results=[]
try:
 with Path('/run/lock/chromapad-rolling-backup.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  state=controller.managed_record(image,controller.STATE,c.USB_BYTES)
  controller.require(state is not None and state['state']=='READY', 'A READY owned image is required')
  expected=state['result']['root_sha256']
  with c.usb_image(stage,image=image) as (fat,root):
   path=root/'opt/chromapad-recovery/root.ext4'
   # Advise eviction of this file's clean cached pages only, never global caches.
   # The underlying outer/NFS caches may remain warm; report this limitation.
   for sparse in (False,True):
    with path.open('rb') as stream:
     os.posix_fadvise(stream.fileno(),0,0,os.POSIX_FADV_DONTNEED)
    started=time.monotonic();cpu=time.process_time()
    print('BENCHMARK_START sparse='+str(sparse),flush=True)
    actual=rolling.hash_payload(path,stage=stage,sparse=sparse)
    result=json.loads((stage/'reports/payload-hash.json').read_text())
    result.update(wall_seconds=round(time.monotonic()-started,3),cpu_seconds=round(time.process_time()-cpu,3),matches_verified_image=actual==expected)
    controller.require(actual==expected, 'Checksum differs from verified backup')
    results.append(result)
    print('BENCHMARK_RESULT '+json.dumps(result),flush=True)
  controller.require(results[0]['sha256']==results[1]['sha256'], 'Checksums differ between methods')
  output={'image':str(image),'build_id':state['result']['build_id'],'cache_policy':'Per-payload POSIX_FADV_DONTNEED before each pass; outer and NAS caches may remain warm','results':results}
  with tempfile.NamedTemporaryFile(mode='w',prefix='chromapad-hash-result-',dir='/var/tmp',delete=False) as report:
   json.dump(output,report,indent=2)
   report.write('\n')
  os.replace(report.name,'/var/tmp/chromapad-payload-hash-benchmark.json')
finally:
 shutil.rmtree(stage)

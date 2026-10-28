#!/usr/bin/python3
"""Explicit disposable-file test for recreation and later automatic growth."""
import fcntl,hashlib,json,os,shutil,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'debian_recovery'));sys.path.insert(0,str(Path(__file__).resolve().parent))
import recovery_build_common as c
import rolling_recovery as rolling
import nas_repack_image as repack
import nas_payload_hash as h
import rolling_backup as controller
from unittest.mock import patch

def main():
    c.private()
    c.LOCAL=Path('/usr/local/lib/chromapad-recovery-builder').resolve()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='chromapad-recreation-fixture-',dir='/var/tmp') as name:
     stage=Path(name);(stage/'reports').mkdir()
     source=stage/'original.img';c.configure_layout(1050624*512+3*1024**3)
     with source.open('xb') as stream:stream.truncate(c.USB_BYTES)
     m=rolling.metadata();rolling.initialize(source,m)
     with c.usb_image(stage,True,image=source) as (_,root):
      bundle=root/'opt/chromapad-recovery';bundle.mkdir(parents=True)
      payload=bundle/'root.ext4'
      with payload.open('xb') as stream:stream.truncate(64*1024**2)
      c.b.command('mkfs.ext4','-q','-F','-U',c.b.ROOT_UUID,payload)
      inner=stage/'seed-inner';inner.mkdir()
      inner_loop=c.b.run('losetup','--find','--show',payload)
      try:
       c.b.command('mount',inner_loop,inner)
       try:
        database=inner/'var/lib/dpkg';(database/'info').mkdir(parents=True)
        (database/'status').write_text('Package: fixture\nStatus: install ok installed\n')
        (database/'info/fixture.list').write_text('/.\n')
       finally:c.b.command('umount',inner)
      finally:c.b.command('losetup','-d',inner_loop)
      (bundle/'manifest.json').write_text('fixture manifest')
      test=root/'fixture';test.write_bytes(b'payload preserved');test.chmod(0o640);os.chown(test,1000,1000)
      os.setxattr(test,'user.fixture',b'preserved');os.link(test,root/'hardlink');(root/'symlink').symlink_to('/fixture')
      sha=repack.file_sha(payload)
      pin=repack.file_sha(bundle/'manifest.json')
     h.IMAGE=source;repack.CANDIDATE=stage/'candidate.img';h.LOCK=stage/'lock'
     req={'operation':'plan','image_bytes':source.stat().st_size,'image_inode':source.stat().st_ino,'start_sector':1050624,
     'sectors':c.USB_PARTS[1][1],**m,'root_sha256':sha,'manifest_sha256':pin,'raw_bytes':64*1024**2}
     plan=repack.repack(req);req.update(operation='repack',new_image_bytes=plan['image_bytes'])
     result=repack.repack(req);assert result['cleanup_complete'] and result['root_sha256']==sha
     assert source.exists() and source.stat().st_size==req['image_bytes']
     c.configure_layout(result['image_bytes'])
     record={'bytes':result['image_bytes'],'source_used_bytes':0}
     owner=repack.CANDIDATE.stat()
     with patch.object(controller,'IMAGE',repack.CANDIDATE),patch.object(controller,'DEST',stage),patch.object(controller,'save_state'):
      controller.ensure_capacity(rolling,stage,record,owner,1024**3)
     assert record['bytes']>result['image_bytes']
     with c.usb_image(stage,image=repack.CANDIDATE) as (_,root):
      test=root/'fixture'
      assert test.read_bytes()==b'payload preserved' and test.stat().st_uid==1000
      assert test.stat().st_ino==(root/'hardlink').stat().st_ino
      assert os.getxattr(test,'user.fixture')==b'preserved'
      assert os.readlink(root/'symlink')=='/fixture'
      assert repack.file_sha(root/'opt/chromapad-recovery/root.ext4')==sha
     print('PASS: recreation, all-content verification, payload hash, automatic growth and metadata preservation',flush=True)

if __name__ == "__main__":
    main()

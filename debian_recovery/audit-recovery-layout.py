import json,struct,uuid
import recovery_build_common as c
from recovery_build_common import b
def audit(stage,m,image=None):
    image=image if image is not None else stage/'recovery-usb.img'
    b.require(image.stat().st_size==c.USB_BYTES,'Wrong image length')
    with image.open('rb') as f:
        mbr=f.read(512);b.require(mbr[510:]==b'\x55\xaa','Missing MBR signature')
        b.require(format(struct.unpack_from('<I',mbr,440)[0],'08x')==m['usb_disk_id'],'Wrong USB disk identity')
        for i,(start,sectors) in enumerate(c.USB_PARTS):
            entry=mbr[446+16*i:462+16*i]
            b.require(struct.unpack_from('<II',entry,8)==(start,sectors) and entry[4]==(0x0c if i==0 else 0x83),'USB partition table differs from build layout')
        b.require(mbr[478:510]==bytes(32),'Unexpected extra partitions')
        f.seek(c.USB_PARTS[0][0]*512);fat=f.read(512)
        ident=format(struct.unpack_from('<I',fat,67)[0],'08X');ident=ident[:4]+'-'+ident[4:]
        b.require(ident==m['usb_fat_uuid'] and ident!=m['fat_uuid'],'FAT identity collision/mismatch')
        f.seek(c.USB_PARTS[1][0]*512+1024);sb=f.read(1024)
        b.require(struct.unpack_from('<H',sb,56)[0]==0xef53 and str(uuid.UUID(bytes=sb[104:120]))==m['usb_root_uuid'],'Wrong runtime ext4 identity')
        b.require(m['usb_root_uuid']!=m['root_uuid'],'USB/eMMC root UUID collision')
    result={'status':'PASS','usb_bytes':c.USB_BYTES,'partitions':[list(x) for x in c.USB_PARTS],'disk_id':m['usb_disk_id'],'root_uuid':m['usb_root_uuid'],'fat_uuid':m['usb_fat_uuid'],'emmc_identity_collision':False}
    b.record(stage/'reports/usb-layout.json',result);print(json.dumps(result,indent=2))


if __name__=='__main__':
    c.private()
    with c.project() as (stage,m):audit(stage,m)

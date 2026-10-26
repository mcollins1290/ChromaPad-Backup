"""Touch shutdown on the host, only after a verified recovery has finished.

Input protocol: https://www.kernel.org/doc/html/latest/input/multi-touch-protocol.html
Queue overruns are resynchronized by reopening and querying the device state.
No restore/write operation is available in this process.
"""
import fcntl,hashlib,json,os,select,struct,subprocess,time
from pathlib import Path
import recovery_ui as ui

BUTTON=(212,320,812,424)
EVENT=struct.Struct('@llHHi')

def require(ok,message):
    if not ok:raise RuntimeError(message)

def run(*args):return subprocess.check_output(args,text=True,timeout=15).strip()

def verified_result(manifest,result,pin):
    require(result.get('status')=='RESTORE_PASS' and result.get('fixture') is False,'No successful physical restore')
    require(result.get('manifest_sha256')==pin,'Completion belongs to another image')
    require(result.get('root_partition_sha256')==manifest['root_payload']['raw_sha256'],'Restored checksum differs')
    for key in ('protected_regions_unchanged','filesystems_clean','boot_entry_verified','cleanup_complete'):
        require(result.get(key) is True,'Recovery incomplete: '+key)

def completed_recovery():
    """Read-only gate, checked at startup and again on the actual shutdown tap."""
    state=dict(line.split('=',1) for line in run('systemctl','show','chromapad-recovery.service','-p','MainPID','-p','Result','-p','SubState').splitlines())
    require(state=={'MainPID':'0','Result':'success','SubState':'exited'},'Recovery is still running or did not finish successfully')
    bundle=Path('/opt/chromapad-recovery')
    release=json.loads((bundle/'release.json').read_text());pin=release['manifest_sha256']
    raw=(bundle/'manifest.json').read_bytes()
    require(hashlib.sha256(raw).hexdigest()==pin,'Manifest pin mismatch')
    manifest=json.loads(raw);build=manifest['build_id']
    require(Path(build).name==build and build.startswith('debian-recovery-'),'Invalid build identity')
    require(run('findmnt','-n','-o','UUID','/')==manifest['usb_root_uuid'],'Not the recovery USB')
    require('chromapad.recovery='+build in Path('/proc/cmdline').read_text().split(),'Not the expected recovery boot')
    state=Path('/var/lib/chromapad-recovery')/build
    result=json.loads((state/'completed.json').read_text())
    verified_result(manifest,result,pin)
    attempt=json.loads((state/'attempt.json').read_text());name=attempt['run']
    require(Path(name).name==name and name.startswith('restore-'),'Invalid attempt identity')
    require(json.loads((state/name/'result.json').read_text())==result,'Completion records disagree')
    require(not (state/name/'failure.json').exists(),'Recovery failure recorded')
    require(Path('/sys/class/block/mmcblk1/device/cid').read_text().strip()==manifest['target']['cid'],'Unexpected eMMC identity')
    for suffix in ('','p1','p2','boot0','boot1'):
        require(Path('/sys/class/block/mmcblk1'+suffix+'/ro').read_text().strip()=='1','eMMC cleanup is incomplete')
    return result

def inside(x,y):return BUTTON[0]<=x<BUTTON[2] and BUTTON[1]<=y<BUTTON[3]

class TouchGesture:
    """One complete, single-finger press/release inside the button is a tap."""
    def __init__(self,slots,current=0):
        self.slots=[dict(x) for x in slots];self.slot=current
        self.ready=not any(x['id']>=0 for x in slots)
        self.contact=None;self.pressed=False

    def feed(self,kind,code,value):
        if kind==0 and code==3:return 'resync'  # SYN_DROPPED: never treat loss as release.
        if kind==3:
            if code==0x2f:
                if not 0<=value<len(self.slots):return 'resync'
                self.slot=value
            elif code in (0x35,0x36,0x39):self.slots[self.slot][{0x35:'x',0x36:'y',0x39:'id'}[code]]=value
        if kind!=0 or code!=0:return None
        contacts=[(i,x) for i,x in enumerate(self.slots) if x['id']>=0]
        if not contacts:
            point=self.slots[self.contact[0]] if self.contact is not None else None
            tapped=self.pressed and point is not None and inside(point['x'],point['y'])
            self.contact=None;self.pressed=False;self.ready=True
            return 'tap' if tapped else None
        if len(contacts)!=1:
            self.ready=False;self.pressed=False;return None
        index,point=contacts[0];identity=(index,point['id'])
        if self.ready:
            self.ready=False;self.contact=identity;self.pressed=inside(point['x'],point['y'])
        elif identity!=self.contact or not inside(point['x'],point['y']):self.pressed=False
        return None

def query(fd,number,size):
    data=bytearray(size);fcntl.ioctl(fd,(2<<30)|(size<<16)|(ord('E')<<8)|number,data,True);return bytes(data)

def open_touch():
    for path in sorted(Path('/dev/input').glob('event*')):
        fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
        try:
            identity=struct.unpack('4H',query(fd,0x02,8))
            if identity[:3]!=(3,0x0484,0x5750):os.close(fd);continue
            axes={axis:struct.unpack('6i',query(fd,0x40+axis,24)) for axis in (0x2f,0x35,0x36)}
            require(axes[0x35][1:3]==(0,1024) and axes[0x36][1:3]==(0,600),'Unexpected touchscreen coordinate range')
            require(axes[0x2f][1]==0 and 0<=axes[0x2f][2]<10,'Unexpected touchscreen slots')
            count=axes[0x2f][2]+1;values={}
            for axis in (0x35,0x36,0x39):
                data=bytearray(struct.pack(f'{count+1}i',axis,*([0]*count)))
                fcntl.ioctl(fd,(2<<30)|(len(data)<<16)|(ord('E')<<8)|0x0a,data,True)
                values[axis]=struct.unpack(f'{count+1}i',data)[1:]
            slots=[dict(id=values[0x39][i],x=values[0x35][i],y=values[0x36][i]) for i in range(count)]
            return fd,TouchGesture(slots,axes[0x2f][0])
        except BaseException:
            os.close(fd);raise
    raise RuntimeError('Recovery touchscreen not found')

def render(pressed=False,shutting_down=False,error=None):
    from PIL import ImageDraw,ImageFont
    image=ui.render({'status':'complete'});draw=ImageDraw.Draw(image)
    draw.rectangle((32,310,992,530),fill='#101923')
    font=ImageFont.truetype(ui.FONT,36);small=ImageFont.truetype(ui.FONT,26)
    if shutting_down:
        draw.text((48,342),'Shutting down...',font=font,fill='#64dc9b')
        draw.text((48,406),'Wait until the pad has fully shut down.',font=small,fill='#ecf2f7')
        draw.text((48,454),'Then remove USB and turn the pad back on.',font=small,fill='#ecf2f7')
    else:
        draw.rounded_rectangle(BUTTON,radius=18,fill='#a0f1bc' if pressed else '#64dc9b')
        label='Shut down';width=draw.textlength(label,font=font)
        draw.text(((1024-width)/2,348),label,font=font,fill='#101923')
        draw.text((48,454),'After shutdown, remove the USB.',font=small,fill='#ecf2f7')
        draw.text((48,494),'Then turn the pad back on to start Debian.',font=small,fill='#ecf2f7')
    if error:
        draw.rectangle((32,532,992,590),fill='#101923')
        draw.text((48,542),'Shutdown failed. Please try again or use SSH.',font=small,fill='#ff9292')
    return image

def poweroff():
    completed_recovery()
    subprocess.run(['systemctl','poweroff'],check=True,timeout=20)

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview',type=Path)
    args=parser.parse_args()
    if args.preview:render().save(args.preview);return
    completed_recovery()
    fb=ui.Framebuffer();fd=None
    try:
        fd,touch=open_touch();fb.draw(render());last=False;error=None
        print('Verified recovery complete; touchscreen shutdown button ready.',flush=True)
        while True:
            if not select.select([fd],[],[],1)[0]:continue
            data=os.read(fd,EVENT.size*64)
            require(data and len(data)%EVENT.size==0,'Touchscreen disconnected or partial event')
            for _,_,kind,code,value in EVENT.iter_unpack(data):
                event=touch.feed(kind,code,value)
                if event=='resync':
                    os.close(fd);fd=None;fd,touch=open_touch();last=False;fb.draw(render(error=error));break
                if event=='tap':
                    fb.draw(render(shutting_down=True))
                    try:
                        poweroff()
                        print('User tapped Shut down; clean poweroff requested.',flush=True)
                        # Remain on the shutdown screen until systemd terminates us.
                        while True:time.sleep(1)
                    except Exception as exc:
                        error=str(exc);print('Shutdown request failed: '+error,flush=True)
                        fb.draw(render(error=error));last=False
                if touch.pressed!=last:
                    last=touch.pressed;fb.draw(render(pressed=last,error=error))
    finally:
        if fd is not None:os.close(fd)
        fb.close()

if __name__=='__main__':main()

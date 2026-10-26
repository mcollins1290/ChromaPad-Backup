"""Large recovery progress display. Display failures never interrupt recovery.

Framebuffer access is confined to this module. It cannot open block devices.
The same renderer produces PNG previews without touching a screen.
"""
import ctypes, datetime, json, os, threading, time
from pathlib import Path

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
STATE = Path('/run/chromapad-recovery-progress.json')

def render(state, size=(1024, 600), elapsed=0):
    from PIL import Image, ImageDraw, ImageFont
    scale = min(size[0]/1024, size[1]/600)
    canvas = Image.new('RGB', (1024,600), '#101923')
    draw = ImageDraw.Draw(canvas)
    fonts = {n: ImageFont.truetype(FONT,n) for n in (22,26,30,36,44,58)}
    color = '#4dc7ef'
    status = state.get('status','working')
    if status == 'complete': color = '#64dc9b'
    elif status == 'failed': color = '#ff9292'
    draw.text((48,30), 'CO PRINT  /  DEBIAN RECOVERY', font=fonts[22], fill='#a9bbc9')
    draw.line((48,74,976,74), fill='#324151', width=2)
    title = {'complete':'Recovery complete', 'failed':'Recovery stopped', 'probe':'Checks passed'}.get(status, state.get('title','Preparing recovery'))
    draw.text((48,104), title, font=fonts[44], fill=color)
    def wrapped(text, y, font=30, maxwidth=920, limit=3):
        words = str(text).split(); lines=[]; line=''
        for word in words:
            test=(line+' '+word).strip()
            if draw.textlength(test,font=fonts[font])>maxwidth and line:
                lines.append(line);line=word
            else:line=test
        if line:lines.append(line)
        for line in lines[:limit]:
            draw.text((48,y),line,font=fonts[font],fill='#ecf2f7');y+=font+12
        return y
    if status == 'complete':
        wrapped('Debian was restored and verified.',182)
        draw.rounded_rectangle((48,252,976,300), radius=12, fill='#234d3d')
        draw.text((68,257),'All checks passed',font=fonts[26],fill=color)
        for y,text in [(340,'1. Shut down the pad.'),(388,'2. Remove the recovery USB.'),(436,'3. Turn on the pad to start Debian.')]:
            draw.text((48,y),text,font=fonts[30],fill='#ecf2f7')
    elif status == 'failed':
        wrapped(state.get('detail','Recovery could not finish.'),182,font=26,limit=3)
        wrapped('Keep the USB connected. Leave the pad on.',340)
        wrapped('Read the saved logs before trying recovery again.',392,font=26)
        wrapped('Do not assume Debian is ready to boot.',446,font=26)
    elif status == 'probe':
        wrapped('This USB is in read-only test mode.',190)
        wrapped('No changes were made to eMMC.',244)
        wrapped('The backup and device checks passed.',340)
    else:
        step=state.get('step',0)
        wrapped(('Step '+str(step)+' of 6  -  ' if step else '')+state.get('detail','Checking the recovery tools.'),180,font=30,limit=2)
        done,total=state.get('done',0),state.get('total',0)
        draw.rounded_rectangle((48,316,976,354),radius=12,fill='#263748')
        if total and total>0:
            ratio=max(0,min(1,done/total));end=48+int(928*ratio)
            if end>60:draw.rounded_rectangle((48,316,end,354),radius=12,fill=color)
            draw.text((48,380),str(int(ratio*100))+'%',font=fonts[58],fill='#ffffff')
            draw.text((220,400),f'{done/1024**3:.2f} of {total/1024**3:.2f} GiB',font=fonts[26],fill='#b9c9d7')
        else:
            x=48+int((time.monotonic()%3)/3*740)
            draw.rounded_rectangle((x,316,x+180,354),radius=12,fill=color)
            draw.text((48,390),'Working...',font=fonts[36],fill='#ffffff')
        draw.text((48,470),f'Elapsed {int(elapsed)//60:02d}:{int(elapsed)%60:02d}',font=fonts[26],fill='#a9bbc9')
        draw.text((48,535),'Keep power connected. Do not remove the USB.',font=fonts[26],fill='#f5ce80')
    if status in ('complete','failed','probe'):
        draw.text((48,542),'Detailed logs are saved on the recovery USB.',font=fonts[22],fill='#a9bbc9')
    if size!=(1024,600):canvas=canvas.resize(size,Image.Resampling.LANCZOS)
    return canvas

class Bitfield(ctypes.Structure):
    _fields_=[('offset',ctypes.c_uint32),('length',ctypes.c_uint32),('msb_right',ctypes.c_uint32)]

class VarInfo(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint32) for name in ('xres','yres','xres_virtual','yres_virtual','xoffset','yoffset','bits_per_pixel','grayscale')]+[(name,Bitfield) for name in ('red','green','blue','transp')]+[(name,ctypes.c_uint32) for name in ('nonstd','activate','height','width','accel_flags','pixclock','left_margin','right_margin','upper_margin','lower_margin','hsync_len','vsync_len','sync','vmode','rotate','colorspace')]+[('reserved',ctypes.c_uint32*4)]

class FixInfo(ctypes.Structure):
    _fields_=[('id',ctypes.c_char*16),('smem_start',ctypes.c_ulong),('smem_len',ctypes.c_uint32),('type',ctypes.c_uint32),('type_aux',ctypes.c_uint32),('visual',ctypes.c_uint32),('xpanstep',ctypes.c_uint16),('ypanstep',ctypes.c_uint16),('ywrapstep',ctypes.c_uint16),('line_length',ctypes.c_uint32),('mmio_start',ctypes.c_ulong),('mmio_len',ctypes.c_uint32),('accel',ctypes.c_uint32),('capabilities',ctypes.c_uint16),('reserved',ctypes.c_uint16*2)]

class Framebuffer:
    def __init__(self):
        import fcntl, mmap
        self.fd=os.open('/dev/fb0',os.O_RDWR|os.O_CLOEXEC)
        try:
            self.var=VarInfo();self.fix=FixInfo()
            fcntl.ioctl(self.fd,0x4600,self.var)
            fcntl.ioctl(self.fd,0x4602,self.fix)
            v,f=self.var,self.fix
            if not (v.bits_per_pixel==32 and (v.red.offset,v.green.offset,v.blue.offset)==(16,8,0) and all(x.length==8 and not x.msb_right for x in (v.red,v.green,v.blue)) and v.xres==1024 and v.yres==600 and not v.grayscale and f.type==0 and f.visual==2):
                raise RuntimeError('Unexpected framebuffer format; using journal progress')
            if f.line_length<(v.xoffset+v.xres)*4 or f.smem_len<f.line_length*(v.yoffset+v.yres):raise RuntimeError('Framebuffer bounds invalid')
            self.memory=mmap.mmap(self.fd,f.smem_len)
            with open('/dev/tty1','wb',buffering=0) as tty:
                fcntl.ioctl(tty,0x5606,1)  # VT_ACTIVATE
                fcntl.ioctl(tty,0x5607,1)  # VT_WAITACTIVE: let fbcon take scanout before graphics mode
                fcntl.ioctl(tty,0x4B3A,1)  # KDSETMODE, KD_GRAPHICS
        except BaseException:
            os.close(self.fd);raise

    def draw(self,image):
        raw=image.convert('RGBA').tobytes('raw','BGRA');v,f=self.var,self.fix
        for y in range(v.yres):
            offset=(y+v.yoffset)*f.line_length+v.xoffset*4
            self.memory[offset:offset+v.xres*4]=raw[y*v.xres*4:(y+1)*v.xres*4]

    def close(self):
        self.memory.close();os.close(self.fd)
        # Leave the final result visible. Shutdown/next boot resets the console.

class RecoveryScreen:
    def __init__(self,enabled=True):
        self.enabled=enabled;self.state={};self.started=time.monotonic();self.lock=threading.Lock();self.stop=threading.Event();self.fb=None
        if enabled:
            try:self.fb=Framebuffer()
            except Exception as exc:print('Recovery display unavailable: '+str(exc),flush=True)
            self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def update(self,title,step=0,done=0,total=0,detail='',status='working'):
        with self.lock:self.state={'title':title,'step':step,'done':done,'total':total,'detail':detail,'status':status,'updated_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}

    def paint(self):
        with self.lock:state=dict(self.state)
        if not state:return
        try:
            pending=STATE.with_suffix('.tmp');pending.write_text(json.dumps(state)+'\n');os.replace(pending,STATE)
        except OSError:pass
        if self.fb:
            try:self.fb.draw(render(state,elapsed=time.monotonic()-self.started))
            except Exception as exc:
                print('Recovery display update failed: '+str(exc),flush=True)
                try:self.fb.close()
                except Exception:pass
                self.fb=None

    def loop(self):
        while not self.stop.wait(0.5):self.paint()

    def finish(self):
        if not self.enabled:return
        self.stop.set();self.thread.join(timeout=3);self.paint()
        if self.fb:self.fb.close();self.fb=None

def preview(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    examples=[('checking',dict(title='Checking the backup',step=2,done=7*1024**3,total=13950255104,detail='Verifying the complete Debian filesystem.')),
              ('restoring',dict(title='Restoring Debian',step=4,done=8*1024**3,total=13950255104,detail='Copying your saved system to eMMC.')),
              ('verifying',dict(title='Verifying the restore',step=5,done=11*1024**3,total=13950255104,detail='Reading back every byte from eMMC.')),
              ('complete',dict(status='complete')),('failure',dict(status='failed',detail='The restored filesystem checksum did not match.'))]
    for name,state in examples:render(state,elapsed=563).save(directory/(name+'.png'))

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--preview',type=Path,required=True);a=p.parse_args();preview(a.preview)

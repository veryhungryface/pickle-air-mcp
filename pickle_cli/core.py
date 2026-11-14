import base64, contextlib, fcntl, hashlib, json, os, re, selectors, shlex, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path
import xml.etree.ElementTree as ET
from .adb import ADB,CLIError
PACKAGE='com.pickle.client'
LABELS={'attach':['첨부 추가','Add attachment'],'files':['파일','Files','File'], 'send':['보내기','Send'], 'root':['루트 표시','Show roots'], 'downloads':['다운로드','Downloads'], 'savefile':['파일에 저장','Save to Files','Save to files'], 'save':['저장','Save','SAVE'], 'savephoto':['Save photo','사진 저장'], 'back':['Back','뒤로'], 'mypickle':['My Pickle','나의 피클','내 피클','마이 피클'], 'chat':['Chat','채팅','대화'], 'tickets':['Reset tickets','초기화권','한도 초기화권','리셋 티켓'], 'order':['Ticket order','사용 순서','티켓 사용 순서']}
SAFE_ASCII=re.compile(r"[A-Za-z0-9 .,!?_:/()+=@#\-]*\Z")
FILE_SIZE=re.compile(r'(?:[^·•\n]{1,24} [·•] ?)?[\d.,]+ ?(B|KB|MB|GB) [·•] ?\S+')  # file card subtitle, e.g. '77B · txt' or '2 pages · 15.9MB · pdf'
def card_ext(info):
    """Extension shown at the end of a file card subtitle ('... · pdf' -> 'pdf'), or ''."""
    m=re.search(r'[·•] ?([A-Za-z0-9]{1,8})\Z',info or '');return m.group(1).lower() if m else ''
def card_name(label,info):
    """Local fix: the app shows some file names without their extension. Report the full name."""
    ext=card_ext(info);return label if not ext or label.lower().endswith('.'+ext) else label+'.'+ext
# Local addition: names of files the agent creates are often Korean or contain spaces. They are only
# matched on screen and used as the local file name, so allow anything that cannot form a path.
# Progress indicators the app shows while the agent works; never part of a reply.
STATUS=re.compile(r'(?:Thinking|Typing|Working|Searching|Reading|Analyzing|Generating|Calling|생각하는 중|생각 중|입력 중|작업 중|검색 중)[^\n]{0,30}(?:\.\.\.|…)')
SAFE_DL=re.compile(r'(?!\.)[^\x00-\x1f/\\:]{1,120}\Z')
SAFE_NAME=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z')

def digest(data):return hashlib.sha256(data).hexdigest()
def bounds(n):
    a=list(map(int,re.findall(r'\d+',n.get('bounds',''))))
    if len(a)!=4:raise CLIError('Missing UI bounds')
    return a

def center(n):
    a=bounds(n);return (a[0]+a[2])//2,(a[1]+a[3])//2

def atomic_json(path,obj):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.journal-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(obj,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        dfd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

class EndpointLock:
    """One lock across custom journal directories and all UI commands."""
    def __init__(self,port):self.port=port;self.fd=None
    def __enter__(self):
        root=Path.home()/'.local/state/pickle-air-cli'
        if root.is_symlink():raise CLIError('State directory must not be a symlink')
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.fd=os.open(root/('endpoint-'+str(self.port)+'.lock'),os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd);self.fd=None
            raise CLIError('Another CLI command is using this emulator')
        return self
    def __exit__(self,*args):
        if self.fd is not None:os.close(self.fd)

class Journal:
    def __init__(self,root,request_id,fingerprint):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',request_id):raise CLIError('Invalid request ID')
        self.root=Path(root).expanduser();self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.root/(request_id+'.json');self.fingerprint=fingerprint
    @contextlib.contextmanager
    def lock(self):
        fd=os.open(self.root/'operation.lock',os.O_CREAT|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise CLIError('Another CLI operation is running')
            if self.path.exists():raise CLIError('Request ID already recorded; check chat/journal, never resend blindly')
            self.set('prepared');yield
        finally:os.close(fd)
    def set(self,state,**extra):atomic_json(self.path,{'state':state,'fingerprint':self.fingerprint,'updated':time.time(),**extra})

class Pickle:
    def __init__(self,adb,agent):self.adb,self.agent=adb,agent
    # Local addition: pixel constants below were written for 720x1280 at 240 dpi. Layout is in dp,
    # so scale them by the real density; the right margin is measured from the real width.
    def scale(self):
        if not hasattr(self,'_k'):
            out=self.adb.shell('wm size; wm density')
            out=out if isinstance(out,str) else ''
            size=re.findall(r'size: (\d+)x(\d+)',out);den=re.findall(r'density: (\d+)',out)
            # Unreadable size: keep the upstream 720x1280 / 240 dpi assumptions.
            self._w,self._k=(int(size[-1][0]),int(den[-1])/240) if size and den else (720,1.0)
        return self._k
    def px(self,v):return round(v*self.scale())
    def width(self):self.scale();return self._w
    def mine(self,n):b=bounds(n);return b[0]>=self.px(200) or b[2]>=self.width()-self.px(60)
    def note(self,text):
        root=Path.home()/'.local/state/pickle-air-cli'
        if not (root/'trace.on').exists():return
        try:
            with open(root/'trace.log','a') as f:f.write(json.dumps({'t':round(time.time(),1),'note':text},ensure_ascii=False)+'\n')
        except OSError:pass
    def trace(self,nodes):
        # Local debugging aid: create ~/.local/state/pickle-air-cli/trace.on to log what each snapshot saw.
        root=Path.home()/'.local/state/pickle-air-cli'
        if not (root/'trace.on').exists():return
        try:
            with open(root/'trace.log','a') as f:
                f.write(json.dumps({'t':round(time.time(),1),'desc':sorted({n.get('content-desc') for n in nodes if n.get('content-desc')}),
                    'texts':[(n.get('text') or '')[:40] for n in nodes if n.get('text')][-6:],'n':len(nodes),
                    'classes':sorted({n.get('class').split('.')[-1] for n in nodes})},ensure_ascii=False)+'\n')
        except OSError:pass
    def snapshot(self):
        last=None;dismissed=False
        for _ in range(4):
            try:
                text=self.adb.shell('uiautomator dump /dev/tty')
                start=text.find('<?xml');end=text.rfind('</hierarchy>')
                if start<0 or end<0:raise CLIError('UI not idle or hierarchy unavailable')
                nodes=list(ET.fromstring(text[start:end+12]).iter('node'));self.trace(nodes)
                # Local fix: Google Play's "rate this app" sheet can pop up over the chat at any moment and
                # made a sent request look failed. Close exactly that dialog with Back (once) and look again.
                if not dismissed and any(n.get('package')=='com.android.vending' for n in nodes) and 'inappreviewdialog' in self.adb.shell('dumpsys window | grep mCurrentFocus || true'):
                    dismissed=True;self.note('closed Play in-app review dialog');self.adb.shell('input keyevent 4');time.sleep(1);continue
                return nodes
            except (CLIError,ET.ParseError) as e:last=e;time.sleep(0.5)
        raise CLIError(str(last))
    def verify(self,nodes):
        packages={n.get('package') for n in nodes if n.get('package')}
        if PACKAGE not in packages or any(p not in {PACKAGE,'com.android.systemui'} for p in packages):raise CLIError('Pickle chat is not foreground')
        if not any(n.get('text')==self.agent and bounds(n)[1]<self.px(300) for n in nodes):raise CLIError('Expected agent is not visible: '+self.agent)
        return nodes
    def choose(self,nodes,key,values,package=None):
        found=[n for n in nodes if n.get(key) in values and (package is None or n.get('package')==package)]
        if len(found)!=1:raise CLIError('UI target missing/ambiguous: '+repr(values))
        return found[0]
    def tap(self,node):
        x,y=center(node);self.adb.shell(f'input tap {x} {y}')
    def until(self,fn,timeout=12):
        """Local fix: screens (file picker, menus) can take seconds to appear. Retry a lookup until
        it succeeds; taps only ever happen after a successful lookup, so nothing is tapped twice."""
        deadline=time.monotonic()+timeout
        while True:
            try:return fn()
            except CLIError:
                if time.monotonic()>=deadline:raise
                time.sleep(0.7)
    def tap_label(self,kind,desc=False):
        self.tap(self.until(lambda:self.choose(self.snapshot(),'content-desc' if desc else 'text',LABELS[kind])))
    def field(self,nodes):return self.choose(nodes,'class',['android.widget.EditText'],PACKAGE)
    def empty(self):
        nodes=self.verify(self.snapshot())
        if self.field(nodes).get('text') or any(n.get('content-desc') in ['첨부 삭제','Remove attachment'] for n in nodes):raise CLIError('Existing draft/attachment found; preserved, nothing sent')
        return nodes
    # --- Local addition: talk to Android's clipboard service directly (shell may read and write it).
    # No Mac clipboard sync, no window activation: works with the Mac screen locked and cannot leak
    # keystrokes. The parcel layout is the Android 13 one; every write is verified by reading back,
    # and callers fall back to the Mac-clipboard path when that check fails.
    @staticmethod
    def _s8(b):
        raw=b+b'\0';raw+=b'\0'*(-len(raw)%4)
        return [len(b)]+[int.from_bytes(raw[i:i+4],'little',signed=True) for i in range(0,len(raw),4)]
    @staticmethod
    def _words(out):
        """Hex words of a `service call` reply, ignoring the printable-text column."""
        words=[]
        for line in out.splitlines():
            m=re.match(r'\s*0x[0-9a-f]{8}: ((?:[0-9a-f]{8} ?)+)',line) or re.search(r'Parcel\(((?:[0-9a-f]{8} ?)+)',line)
            if m:words+=[int(w,16) for w in m.group(1).split()]
        return words
    def clip_get(self):
        out=self.adb.shell('service call clipboard 4 s16 com.android.shell i32 -1 i32 0')
        words=self._words(out)
        try:
            i=0
            if words[i]!=0:raise CLIError('Clipboard service returned an error')
            if words[i+1]==0:return None
            i+=2
            def s8(i):
                n=words[i]
                if n==0xffffffff:return None,i+1
                k=(n+1+3)//4;raw=b''.join(w.to_bytes(4,'little') for w in words[i+1:i+1+k])[:n]
                return raw.decode('utf-8'),i+1+k
            i+=1;_,i=s8(i)                       # label
            for _ in range(words[i]):            # mime types (UTF-16)
                n=words[i+1];i+=1+(0 if n==0xffffffff else ((n+1)*2+3)//4)
            i+=1
            if words[i]!=0xffffffff:raise CLIError('Unexpected clipboard extras')
            i+=1+2+1+1                           # extras, timestamp, styled flag, classification
            n=words[i];i+=1+((n+3)//4+1 if n else 0)  # confidence bundle
            if words[i]!=0:raise CLIError('Unexpected clipboard icon')
            if words[i+1]<1:return ''
            text,_=s8(i+3)
            return text
        except (IndexError,UnicodeDecodeError) as e:raise CLIError('Clipboard parcel not understood') from e
    def clip_set(self,text):
        now=int(time.time()*1000)
        w=[1,1]+self._s8(b'simpleText')+[1,10]+[int.from_bytes(('text/plain'.encode('utf-16-le')+b'\0\0\0\0')[i:i+4],'little',signed=True) for i in range(0,24,4)]
        w+=[-1,now&0x7fffffff,now>>32,0,1,0,0,1,1]+self._s8(text.encode('utf-8'))+[-1,0,0,0,0]
        cmd='service call clipboard 1 '+' '.join('i32 %d'%x for x in w)+' s16 com.android.shell i32 -1 i32 0'
        if len(cmd)<3500:out=self.adb.shell(cmd)
        else:
            # Too long for one ADB command: stage it as a script inside the shell-only temp directory.
            path='/data/local/tmp/pickle-clip-'+uuid.uuid4().hex[:12];enc=base64.b64encode(cmd.encode()).decode()
            try:
                for i in range(0,len(enc),2400):self.adb.shell("printf '%s' "+shlex.quote(enc[i:i+2400])+(' > ' if i==0 else ' >> ')+path+'.b64')
                out=self.adb.shell('base64 -d '+path+'.b64 > '+path+' && sh '+path)
            finally:
                try:self.adb.shell('rm -f '+path+' '+path+'.b64')
                except CLIError:pass
        if self._words(out)[:1]!=[0]:raise CLIError('Clipboard service rejected the text')
        if self.clip_get()!=text:raise CLIError('Clipboard did not take the text')
    @contextlib.contextmanager
    def keep_mac_clipboard(self):
        """The emulator mirrors its clipboard to the Mac; put the user's text clipboard back afterwards."""
        ok=sys.platform=='darwin' and shutil.which('pbpaste') and shutil.which('pbcopy');env={**os.environ,'LC_CTYPE':'UTF-8'};prev=None
        if ok:
            try:prev=subprocess.run(['pbpaste'],capture_output=True,timeout=10,env=env).stdout
            except (OSError,subprocess.SubprocessError):prev=None
        try:yield
        finally:
            if prev is not None:
                try:time.sleep(0.4);subprocess.run(['pbcopy'],input=prev,timeout=10,env=env)
                except (OSError,subprocess.SubprocessError):pass
    def clear_field(self,length):
        """Delete our own wrong paste. Ctrl+End puts the cursor after the last line (plain End only
        reaches the end of the current line), then backspace over everything; repeat if needed."""
        for _ in range(3):
            self.adb.shell('input keycombination 113 123')
            for _ in range(0,length+3,400):self.adb.shell('input keyevent '+'67 '*400)
            time.sleep(0.5)
            left=self.field(self.verify(self.snapshot())).get('text') or ''
            if not left:return
            length=len(left)
        raise CLIError('Could not clear a wrong paste; draft left for inspection, nothing sent')
    def paste_once(self,message):
        helper=Path(__file__).with_name('clipboard.swift')
        p=subprocess.Popen(['swift',str(helper)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            p.stdin.write(base64.b64encode(message.encode()).decode()+'\n');p.stdin.flush()
            sel=selectors.DefaultSelector();sel.register(p.stdout,selectors.EVENT_READ)
            if not sel.select(60):raise CLIError('Clipboard helper timed out; no send attempted')
            ready=p.stdout.readline().strip()
            if ready!='READY':raise CLIError('Unicode helper unavailable: '+p.stderr.read()[-500:])
            # Paste inside Android; re-check that the field is still empty before every try so the
            # text can never be pasted twice unnoticed.
            for _ in range(5):
                if self.field(self.verify(self.snapshot())).get('text'):break
                self.adb.shell('input keyevent 279');time.sleep(1.2)
        finally:
            if p.poll() is None:
                try:
                    p.stdin.write('DONE\n');p.stdin.flush();_o,err=p.communicate(timeout=10)
                    self.note('clipboard helper: '+' '.join(l for l in (err or '').splitlines() if l.startswith('diag:')))
                except (BrokenPipeError,subprocess.TimeoutExpired):p.terminate()
    def text(self,message):
        if not message or '\0' in message or len(message)>12000:raise CLIError('Message must contain 1–12000 characters, without NUL')
        nodes=self.verify(self.snapshot());f=self.field(nodes)
        if f.get('text'):raise CLIError('Existing text draft preserved')
        self.tap(f)
        if SAFE_ASCII.fullmatch(message):
            for offset in range(0,len(message),1000):
                self.adb.shell('input text '+shlex.quote(message[offset:offset+1000].replace(' ','%s')))
        else:
            if sys.platform!='darwin' or not shutil.which('swift'):raise CLIError('Unicode needs macOS + installed Swift tools; no installation attempted')
            # Local fix: the emulator only picks up the Mac clipboard when its window is activated, and
            # now and then it still holds the previous clipboard. The field was empty when we started,
            # so anything in it now is ours: clear a wrong paste and try again (never sends anything).
            direct=False
            try:
                with self.keep_mac_clipboard():
                    self.clip_set(message)
                    for _ in range(3):
                        if self.field(self.verify(self.snapshot())).get('text'):break
                        self.adb.shell('input keyevent 279');time.sleep(1.0)
                current=self.field(self.verify(self.snapshot())).get('text') or ''
                direct=current==message
                if not direct and current:self.clear_field(len(current))
            except CLIError as e:self.note('direct clipboard paste failed: '+str(e))
            for attempt in range(0 if direct else 3):
                self.paste_once(message)
                current=self.field(self.verify(self.snapshot())).get('text') or ''
                if current==message:break
                self.note('paste attempt %d mismatched (%d chars in field)'%(attempt+1,len(current)))
                if current:self.clear_field(len(current))
        if self.field(self.verify(self.snapshot())).get('text')!=message:raise CLIError('Input mismatch; draft left for inspection, nothing sent')
    def read(self):
        nodes=self.verify(self.snapshot());field=self.field(nodes);top=bounds(field)[1]
        result=[]
        for n in nodes:
            if n.get('class')=='android.widget.TextView' and n.get('text') and self.px(235)<bounds(n)[1]<top:
                result.append({'text':n.get('text'),'bounds':n.get('bounds')})
        return result
    # --- Local addition (not in upstream 0.1.0): scroll back to read text above the visible screen.
    def page(self):
        """Visible chat items, top to bottom, as (text, outgoing)."""
        items=sorted((bounds(m)[1],bounds(m)[0],self.mine(m),m['text']) for m in self.read())
        return [(t,mine) for _,_,mine,t in items]
    def collect(self,pages,stop=None):
        """Scroll toward older messages up to `pages` times, merging overlapping pages in order.
        Stops early at the outgoing message `stop` or at the top of the history. Read-only apart
        from scrolling; always scrolls back to the newest message afterwards."""
        nodes=self.verify(self.snapshot());bottom=bounds(self.field(nodes))[1]
        lo,hi=bottom-self.px(560),bottom-self.px(140);mid=self.width()//2
        if lo<self.px(260):raise CLIError('Chat area too small to scroll safely')
        def swipe(older):
            a,b=(lo,hi) if older else (hi,lo)
            self.adb.shell(f'input touchscreen swipe {mid} {a} {mid} {b} 900');time.sleep(0.8)
        acc=self.page();prev=acc;moved=0;reached=False;top=False;gap=False
        try:
            for _ in range(pages):
                if stop is not None and (stop,True) in acc:break
                swipe(True);moved+=1
                cur=self.page()
                if cur==prev:top=True;break
                k=next((k for k in range(min(len(cur),len(acc)),0,-1) if cur[-k:]==acc[:k]),0)
                if k==0:gap=True
                acc=cur[:len(cur)-k]+acc;prev=cur
            reached=stop is not None and (stop,True) in acc
        finally:
            try:
                last=None
                for _ in range(moved+3):
                    if not moved:break
                    swipe(False);cur=self.page()
                    if cur==last:break
                    last=cur
                self.to_latest()
            except CLIError:pass
        return {'items':acc,'reached':reached,'top':top,'gap':gap,'scrolled':moved}
    def to_latest(self):
        """Tap the app's own jump-to-newest button when the chat is scrolled up."""
        b=[n for n in self.snapshot() if n.get('content-desc') in ['Back to latest','최신 메시지로 이동'] and n.get('package')==PACKAGE]
        if len(b)==1:self.tap(b[0]);time.sleep(1)
    def tree(self):
        last=None
        for _ in range(3):
            try:
                text=self.adb.shell('uiautomator dump /dev/tty')
                start=text.find('<?xml');end=text.rfind('</hierarchy>')
                if start<0 or end<0:raise CLIError('UI not idle or hierarchy unavailable')
                return ET.fromstring(text[start:end+12])
            except (CLIError,ET.ParseError) as e:last=e;time.sleep(0.5)
        raise CLIError(str(last))
    def rows(self):
        """Chat bubbles that can be long-pressed, newest (lowest) first. Each bubble is one row of
        the chat list: the outermost ancestor of its text that is narrower than the screen."""
        root=self.tree();nodes=list(root.iter('node'))
        # Local fix: a slow scroll gesture can open the long-press menu. Close it (only when it is
        # really open) and look again, so its Reply/Copy/Select and emoji rows are never read as messages.
        if self.menu_open(nodes):
            self.adb.shell('input keyevent 4');time.sleep(0.8)
            root=self.tree();nodes=list(root.iter('node'))
            if self.menu_open(nodes):raise CLIError('Message menu is still open')
        self.verify(nodes)
        bottom=bounds(self.field(nodes))[1];width=max(bounds(n)[2] for n in nodes)
        parent={c:q for q in root.iter() for c in q}
        found={}
        for n in nodes:
            if n.get('class')!='android.widget.TextView' or not n.get('text') or n.get('package')!=PACKAGE:continue
            b=bounds(n)
            if b[3]<=self.px(235) or b[1]>=bottom:continue
            row=None;a=n
            while a in parent and a.tag=='node':
                ab=bounds(a)
                if ab[0]>0 or ab[2]<width:row=a
                a=parent[a]
            if row is None:continue
            r=found.setdefault(id(row),{'top':bounds(row)[1],'texts':[],'mine':False,'press':None})
            r['texts'].append((b[1],n.get('text')))
            if b[2]>=width-self.px(60):r['mine']=True
            top=max(b[1],self.px(250));end=min(b[3],bottom-self.px(10))
            if end-top>=10 and (r['press'] is None or top>r['press'][2]):
                r['press']=((b[0]+b[2])//2,(top+end)//2,top)
                # Local fix: a code block swallows the long press. Keep a second point in the bubble's
                # own padding, just right of the text, where the menu does open.
                e=parent.get(n)
                while e is not None and e.tag=='node' and bounds(e)[2]<=b[2]:e=parent.get(e)
                eb=bounds(e) if e is not None and e.tag=='node' else None
                r['alt']=((b[2]+eb[2])//2,(top+end)//2) if eb and self.px(8)<=eb[2]-b[2]<=self.px(60) and eb[2]<width else None
        # Local addition: a picture the agent (or I) sent is a row with no text at all, just a tappable view.
        for n in nodes:
            if n.get('clickable')!='true' or n.get('package')!=PACKAGE or n.get('content-desc') or n.get('text'):continue
            b=bounds(n)
            if b[3]<=self.px(235) or b[1]>=bottom or b[2]-b[0]<self.px(120) or b[3]-b[1]<self.px(120) or b[2]-b[0]>=width:continue
            row=None;a=n
            while a in parent and a.tag=='node':
                ab=bounds(a)
                if ab[0]>0 or ab[2]<width:row=a
                a=parent[a]
            if row is None or id(row) in found or any(c.get('text') for c in row.iter('node')):continue
            top=max(b[1],self.px(250));end=min(b[3],bottom-self.px(10))
            if end-top<self.px(40):continue
            found[id(row)]={'top':bounds(row)[1],'texts':[(b[1],'[image]')],'mine':b[2]>=width-self.px(60),'press':((b[0]+b[2])//2,(top+end)//2,top),'image':True}
        out=[]
        for r in sorted(found.values(),key=lambda r:-r['top']):
            if r['press'] is None:continue
            texts=[t for _,t in sorted(r['texts'])]
            if r.get('image'):
                out.append({'texts':texts,'mine':r['mine'],'press':r['press'][:2],'image':True});continue
            size=[t for t in texts if FILE_SIZE.fullmatch(t)]
            label=[t for t in texts if t not in size and not re.fullmatch(r'[A-Z0-9]{2,5}',t)]
            if size and len(label)==1:
                out.append({'texts':texts,'mine':r['mine'],'press':r['press'][:2],'file':{'name':card_name(label[0],size[0]),'info':size[0]}});continue
            # A centred single line that reaches neither margin is a date separator, not a bubble.
            out.append({'texts':texts,'mine':r['mine'],'press':r['press'][:2],'alt':r.get('alt')})
        return out
    @staticmethod
    def menu_open(nodes):
        return any(n.get('content-desc') in ['More emoji','이모지 더보기'] for n in nodes) or \
            {'Reply','Copy','Select'}<={n.get('text') for n in nodes} or {'답장','복사','선택'}<={n.get('text') for n in nodes}
    def open_copy_menu(self,x,y):
        self.adb.shell(f'input touchscreen swipe {x} {y} {x} {y} 900');time.sleep(1)
        after=self.snapshot()
        menu=[n for n in after if n.get('text') in ['Copy','복사'] and n.get('package')==PACKAGE]
        if len(menu)!=1:
            # Only dismiss when the reaction/menu overlay is really open; never leave the chat.
            if any(n.get('content-desc') in ['More emoji','이모지 더보기'] for n in after):self.adb.shell('input keyevent 4');time.sleep(0.8)
            raise CLIError('Copy menu not found')
        self.tap(menu[0])
    def copy_at(self,x,y,alt=None):
        try:return self._copy_at(x,y)
        except CLIError as e:
            if str(e)!='Copy menu not found' or not alt:raise
        return self._copy_at(*alt)
    def mac_longer(self,cur):
        """Local fix: the emulator's clipboard service hands back at most 4000 bytes of a long copy,
        while the copy mirrored to the Mac is whole. Use the Mac text when it extends this one."""
        if not 3990<=len(cur.encode('utf-8'))<=4000 or sys.platform!='darwin' or not shutil.which('pbpaste'):return cur
        env={**os.environ,'LC_CTYPE':'UTF-8'};head=cur[:-4]
        for _ in range(10):
            try:mac=subprocess.run(['pbpaste'],capture_output=True,timeout=10,env=env).stdout.decode('utf-8','replace')
            except (OSError,subprocess.SubprocessError):break
            if len(mac)>len(cur) and mac.startswith(head):return mac
            time.sleep(0.3)
        self.note('copy may be cut at 4000 bytes');return cur
    def _copy_at(self,x,y):
        """Long-press a bubble and use the app's own Copy: the whole original text (markdown),
        however long. BlueStacks shares its clipboard with macOS, so it is read with pbpaste;
        a previous text clipboard is put back afterwards."""
        try:
            with self.keep_mac_clipboard():
                sentinel='pickle-cli-copy-'+uuid.uuid4().hex
                self.clip_set(sentinel)
                self.open_copy_menu(x,y)
                for _ in range(20):
                    time.sleep(0.3);cur=self.clip_get()
                    if cur is not None and cur!=sentinel:return self.mac_longer(cur)
                raise CLIError('Copied text did not reach the clipboard')
        except CLIError as e:
            if str(e)=='Copy menu not found':raise
            self.note('direct clipboard copy failed: '+str(e))
        if sys.platform!='darwin' or not shutil.which('pbpaste') or not shutil.which('pbcopy'):raise CLIError('Copy needs the macOS clipboard')
        env={**os.environ,'LC_CTYPE':'UTF-8'}
        prev=subprocess.run(['pbpaste'],capture_output=True,timeout=10,env=env).stdout
        sentinel=('pickle-cli-copy-'+uuid.uuid4().hex).encode()
        subprocess.run(['pbcopy'],input=sentinel,timeout=10,check=True,env=env)
        try:
            self.open_copy_menu(x,y)
            for _ in range(20):
                time.sleep(0.3)
                cur=subprocess.run(['pbpaste'],capture_output=True,timeout=10,env=env).stdout
                if cur!=sentinel:return cur.decode('utf-8','replace')
            raise CLIError('Copied text did not reach the Mac clipboard')
        finally:
            subprocess.run(['pbcopy'],input=prev,timeout=10,env=env)
    def copy_last(self):
        self.to_latest()
        for r in self.rows():
            if not r['mine'] and not self.separator(r):return self.copy_at(*r['press'],alt=r.get('alt'))
        raise CLIError('No agent message visible to copy')
    @staticmethod
    def separator(r):return len(r['texts'])==1 and re.fullmatch(r'(Today|Yesterday|오늘|어제|\w+ \d+|\d+월 \d+일).*',r['texts'][0]) is not None and len(r['texts'][0])<40
    def copy_bubbles(self,stop=None,limit=20,pages=40):
        """Copy every agent bubble in full, walking up from the newest one. With `stop`, walk until
        the outgoing message with that text (a reply split over several bubbles is returned whole);
        otherwise take the newest `limit` bubbles. Oldest first in the result."""
        self.to_latest()
        nodes=self.verify(self.snapshot());bottom=bounds(self.field(nodes))[1]
        lo,hi=bottom-self.px(560),bottom-self.px(140);mid=self.width()//2
        if lo<self.px(260):raise CLIError('Chat area too small to scroll safely')
        got=[];files=[];images=0;prev=None;moved=0;reached=False;done=False;same=0
        try:
            for _ in range(pages):
                rows=self.rows()
                sigs=[{t for t in r['texts'] if len(t)>3} or set(r['texts']) for r in rows]
                j=0
                if prev is not None:
                    if sigs==prev:
                        # Local fix: one bubble taller than the screen looks the same page after page.
                        # Keep scrolling a few pages before deciding this is the top of the history.
                        same+=1
                        if len(rows)!=1 or same>=14:break
                        self.adb.shell(f'input touchscreen swipe {mid} {lo} {mid} {hi} 900');time.sleep(0.8);moved+=1
                        continue
                    same=0
                    j=next((k for k in range(min(len(rows),len(prev)),0,-1) if all(sigs[i]&prev[len(prev)-k+i] for i in range(k))),0)
                for r in rows[j:]:
                    if r['mine']:
                        reached=stop is not None and stop in r['texts'];done=True;break
                    if self.separator(r):continue
                    if r.get('image'):
                        images+=1;got.append('[image]');continue
                    if r.get('file'):
                        files.append(r['file']);got.append('[file: %s (%s)]'%(r['file']['name'],r['file']['info']));continue
                    try:text=self.copy_at(*r['press'],alt=r.get('alt'))
                    except CLIError:text='\n'.join(r['texts'])  # no Copy menu on this bubble: keep what is visible
                    got.append(text)
                    if stop is None and len(got)>=limit:done=True;break
                if done:break
                prev=sigs
                self.adb.shell(f'input touchscreen swipe {mid} {lo} {mid} {hi} 900');time.sleep(0.8);moved+=1
        finally:
            try:
                for _ in range(moved):self.adb.shell(f'input touchscreen swipe {mid} {hi} {mid} {lo} 300')
                time.sleep(0.8);self.to_latest()
            except CLIError:pass
        return {'messages':got[::-1],'files':files[::-1],'images':images,'reached':reached}
    def picker_folder(self,folder):
        self.tap_label('root',True)
        self.tap(self.until(self.drawer_downloads))
        self.tap(self.until(lambda:self.choose(self.snapshot(),'text',[folder],'com.android.documentsui')))
    def drawer_downloads(self):
        nodes=self.snapshot()
        candidates=[n for n in nodes if n.get('text') in LABELS['downloads'] and n.get('package')=='com.android.documentsui' and bounds(n)[0]>=self.px(80) and bounds(n)[2]<=self.px(600) and bounds(n)[1]>self.px(120)]
        if len(candidates)!=1:raise CLIError('Downloads drawer entry ambiguous')
        return candidates[0]
    def attach(self,remote_folder,name):
        self.verify(self.snapshot());self.tap_label('attach',True);self.tap_label('files')
        self.picker_folder(remote_folder)
        self.tap(self.until(lambda:self.choose(self.snapshot(),'text',[name],'com.android.documentsui')))
        self.until(lambda:self.verify(self.snapshot()))
    def upload(self,paths,folder):
        target='/sdcard/Download/'+folder
        self.adb.shell('mkdir '+shlex.quote(target))
        out=[]
        for index,(path,data) in enumerate(paths):
            p=Path(path).expanduser()
            if len(data)>8*1024*1024:raise CLIError('Attachment limit: 8 MiB per file')
            name=p.name if SAFE_NAME.fullmatch(p.name) else f'attachment-{index+1}'+p.suffix.lower()
            if not SAFE_NAME.fullmatch(name):raise CLIError('Unsupported attachment extension/name')
            name=f'{index+1:02d}-'+name
            encoded=base64.b64encode(data).decode();tmp=target+'/'+name+'.b64';dest=target+'/'+name
            for i in range(0,max(len(encoded),1),2400):
                self.adb.shell("printf '%s' "+shlex.quote(encoded[i:i+2400])+(' > ' if i==0 else ' >> ')+shlex.quote(tmp))
            self.adb.shell('base64 -d '+shlex.quote(tmp)+' > '+shlex.quote(dest))
            if self.adb.shell('sha256sum '+shlex.quote(dest)).split()[0]!=digest(data):raise CLIError('Uploaded file hash mismatch')
            self.adb.shell('am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d '+shlex.quote('file://'+dest))
            out.append(name)
        return out
    def read_remote(self,remote,size):
        """Local fix: one `base64` of a file near the 16 MiB limit outlasts the ADB timeout. Read big
        files in 512 KiB pieces; the caller compares the SHA-256 of the result with the device's."""
        q=shlex.quote(remote);step=512*1024
        if size<=step:return base64.b64decode(self.adb.shell('base64 '+q),validate=False)
        return b''.join(base64.b64decode(self.adb.shell(f'dd if={q} bs={step} skip={i} count=1 2>/dev/null | base64'),validate=False) for i in range(-(-size//step)))
    def pull(self,remote,expected_size=None):
        previous=None;stable=0;data=None;sha=None
        for _ in range(30):
            try:
                size=int(self.adb.shell('stat -c %s '+shlex.quote(remote)).strip())
                if size>16*1024*1024:raise CLIError('Response exceeds 16 MiB download limit')
                if (expected_size is not None and size!=expected_size) or (size==0 and expected_size!=0):
                    time.sleep(1);continue
                sha=self.adb.shell('sha256sum '+shlex.quote(remote)).split()[0]
                signature=(size,sha)
                stable=stable+1 if signature==previous else 0;previous=signature
                if stable<2:time.sleep(1);continue
                data=self.read_remote(remote,size)
                after=self.adb.shell('sha256sum '+shlex.quote(remote)).split()[0]
                if len(data)==size and digest(data)==sha==after:break
                stable=0;data=None
            except (CLIError,ValueError):
                data=None
            time.sleep(1)
        if data is None:raise CLIError('Response file was not stable/complete; no local output written')
        return data,sha
    def download_image(self,output_dir):
        """Save the newest picture the agent sent: open it in the app's viewer, use its own Save photo
        button (writes to Pictures/Pickle in the emulator), copy that file out and remove it there."""
        out=Path(output_dir).expanduser();out.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.to_latest();row=None
        nodes=self.verify(self.snapshot());bottom=bounds(self.field(nodes))[1];mid=self.width()//2
        for _ in range(12):
            row=next((r for r in self.rows() if r.get('image') and not r['mine']),None)
            if row:break
            self.adb.shell(f'input touchscreen swipe {mid} {bottom-self.px(560)} {mid} {bottom-self.px(140)} 900');time.sleep(0.8)
        if not row:
            try:self.to_latest()
            except CLIError:pass
            raise CLIError('No picture from the agent found in the recent chat')
        folder='/sdcard/Pictures/Pickle'
        listing=lambda:set(self.adb.shell('ls '+folder+' 2>/dev/null || true').split())
        before=listing()
        self.adb.shell('input tap %d %d'%row['press'])
        try:
            self.tap(self.until(lambda:self.choose(self.verify(self.snapshot()),'content-desc',LABELS['savephoto'],PACKAGE)))
            new=set();deadline=time.monotonic()+20
            while not new and time.monotonic()<deadline:
                time.sleep(1);new=listing()-before
            if len(new)!=1:raise CLIError('Saved picture not found in the emulator')
            name=new.pop()
            if not SAFE_NAME.fullmatch(name):raise CLIError('Unexpected saved picture name')
            remote=folder+'/'+name
            data,sha=self.pull(remote)
            dest=out/('pickle-image-'+time.strftime('%Y%m%d-%H%M%S')+(Path(name).suffix.lower() or '.png'))
            fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(data)
            try:self.adb.shell('rm '+shlex.quote(remote))
            except CLIError:pass
            return {'path':str(dest),'bytes':len(data),'sha256':sha}
        finally:
            try:
                back=[n for n in self.snapshot() if n.get('content-desc') in LABELS['back'] and n.get('package')==PACKAGE]
                if len(back)==1 and any(n.get('content-desc') in LABELS['savephoto'] for n in self.snapshot()):self.tap(back[0]);time.sleep(1)
                self.to_latest()
            except CLIError:pass
    # --- Local addition: read the daily usage and reset tickets from My Pickle > Usage. Read-only:
    # the only taps are the My Pickle tab, the Reset tickets row, Back and the Chat tab.
    def usage(self):
        tab=lambda names:lambda:self.choose([n for n in self.snapshot() if bounds(n)[1]>self.px(1100)],'content-desc',names,PACKAGE)
        self.verify(self.snapshot())
        try:
            self.tap(self.until(tab(LABELS['mypickle'])))
            self.tap(self.until(lambda:self.choose(self.snapshot(),'content-desc',LABELS['tickets'],PACKAGE)))
            def screen():
                nodes=self.snapshot()
                if any(n.get('content-desc') in LABELS['mypickle'] for n in nodes):raise CLIError('Usage screen did not open')
                texts=sorted(((bounds(n),n.get('text')) for n in nodes if n.get('class')=='android.widget.TextView' and n.get('text') and n.get('package')==PACKAGE),key=lambda t:(t[0][1],t[0][0]))
                if not any(re.search(r'\d+ ?%',t) for _,t in texts):raise CLIError('Usage screen not readable')
                return nodes,texts
            nodes,texts=self.until(screen)
            pb,pt=next((b,t) for b,t in texts if re.search(r'\d+ ?%',t))
            used=int(re.search(r'(\d+) ?%',pt).group(1))
            below=[(b,t) for b,t in texts if pb[1]<b[1]<pb[1]+self.px(120) and re.match(r'\d',t)]
            counts=[(b,t) for b,t in texts if t.isdigit() and b[1]>pb[1] and b[0]>self.width()//2]
            row=sorted((c for c in counts if abs(c[0][1]-counts[0][0][1])<self.px(20)),key=lambda c:c[0][0]) if counts else []
            if len(row)!=2:raise CLIError('Reset ticket counts not readable')
            switch=[n for n in nodes if n.get('checkable')=='true' and n.get('package')==PACKAGE]
            lab=next((b for b,t in texts if t in LABELS['order']),None)
            order=next((t for b,t in texts if lab and t not in LABELS['order'] and abs(b[1]-lab[1])<self.px(20)),None)
            return {'used_percent':used,'remaining_percent':100-used,'resets_in':below[0][1] if below else None,
                    'full_reset_tickets':int(row[0][1]),'half_reset_tickets':int(row[1][1]),
                    'auto_use_tickets':(switch[0].get('checked')=='true') if len(switch)==1 else None,'auto_use_order':order,
                    'note':'Read-only: no ticket was used or bought.'}
        finally:
            try:
                nodes=self.snapshot()
                back=[n for n in nodes if n.get('content-desc') in LABELS['back'] and n.get('package')==PACKAGE]
                if len(back)==1 and not any(n.get('content-desc') in LABELS['chat'] for n in nodes):self.tap(back[0]);time.sleep(1)
                self.tap(self.until(tab(LABELS['chat'])))
                self.until(lambda:self.field(self.verify(self.snapshot())))
            except CLIError:pass
    def find_file(self,name):
        """Locate a file card by exact name, newest first, scrolling back a few pages if needed.
        Local fix: the app may show the name without its extension ('report' + '... · pdf'), so
        'report.pdf' also matches that card."""
        self.to_latest()
        stem,ext=Path(name).stem,Path(name).suffix[1:].lower()
        for _ in range(12):
            nodes=self.verify(self.snapshot());bottom=bounds(self.field(nodes))[1]
            def card(n):
                if n.get('text')==name:return True
                if not ext or n.get('text')!=stem:return False
                b=bounds(n)
                return any(FILE_SIZE.fullmatch(m.get('text') or '') and card_ext(m.get('text'))==ext and b[1]<=bounds(m)[1]<=b[3]+self.px(100) for m in nodes)
            hit=[n for n in nodes if card(n) and n.get('package')==PACKAGE and self.px(250)<center(n)[1]<bottom-self.px(10)]
            if hit:return nodes,max(hit,key=lambda n:bounds(n)[1])
            mid=self.width()//2
            self.adb.shell(f'input touchscreen swipe {mid} {bottom-self.px(560)} {mid} {bottom-self.px(140)} 900');time.sleep(0.8)
        try:self.to_latest()
        except CLIError:pass
        raise CLIError('File name not found in the recent chat: '+name)
    def download(self,name,output_dir):
        if not SAFE_DL.fullmatch(name):raise CLIError('Download filename must not contain path separators or control characters')
        out=Path(output_dir).expanduser();out.mkdir(mode=0o700,parents=True,exist_ok=True)
        if (out/name).exists():raise CLIError('Output exists; refusing overwrite: '+str(out/name))
        nodes,n=self.find_file(name)
        nearby=[m.get('text','') for m in nodes if m.get('text') and bounds(n)[1]<=bounds(m)[1]<=bounds(n)[3]+self.px(100)]
        # Local fix: keep the extension the card shows when the given name has none.
        info=next((t for t in nearby if FILE_SIZE.fullmatch(t)),'')
        if not Path(name).suffix:name=card_name(name,info)
        dest=out/name
        if dest.exists():raise CLIError('Output exists; refusing overwrite: '+str(dest))
        exact_sizes=[int(m.group(1)) for m in (re.search(r'(?:\A|[·•] ?)(\d+)B [·•]',t) for t in nearby) if m]
        expected_size=exact_sizes[0] if len(exact_sizes)==1 else None
        x,y=center(n);self.adb.shell(f'input touchscreen swipe {x} {y} {x} {y} 900')
        self.tap_label('savefile')
        self.tap_label('root',True)
        self.tap(self.until(self.drawer_downloads));time.sleep(0.8)
        def name_field():
            fields=[n for n in self.snapshot() if n.get('class')=='android.widget.EditText' and n.get('package')=='com.android.documentsui']
            if len(fields)!=1:raise CLIError('Save filename field missing')
            return fields
        fields=self.until(name_field)
        ext=Path(name).suffix;unique='pickle-response-'+uuid.uuid4().hex[:12]+(ext if re.fullmatch(r'\.[A-Za-z0-9]{1,8}',ext) else '')  # typed with ASCII-only input
        self.tap(fields[0]);self.adb.shell('input keycombination 113 29; input text '+shlex.quote(unique))
        nodes=self.snapshot()
        if not any(n.get('class')=='android.widget.EditText' and n.get('text')==unique for n in nodes):raise CLIError('Save filename mismatch')
        self.tap(self.choose(nodes,'text',LABELS['save'],'com.android.documentsui'))
        remote='/sdcard/Download/'+unique
        data,sha=self.pull(remote,expected_size)
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(data)
        try:self.adb.shell('rm '+shlex.quote(remote));self.to_latest()  # do not leave copies in the emulator
        except CLIError:pass
        return {'path':str(dest),'bytes':len(data),'sha256':sha,'device_path':remote}

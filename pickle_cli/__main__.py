import argparse, hashlib, uuid, json, os, shlex, shutil, subprocess, sys, time
from pathlib import Path
from .adb import ADB,CLIError
from .core import Pickle,Journal,digest,SAFE_NAME,SAFE_DL,LABELS,EndpointLock,bounds,STATUS

def output(value):print(json.dumps(value,ensure_ascii=False,indent=2),flush=True)
def parser():
    p=argparse.ArgumentParser(description='Unofficial local BlueStacks Air / Pickle CLI')
    p.add_argument('--port',type=int,default=5555,help='127.0.0.1 port only; default 5555')
    p.add_argument('--state-dir',default='~/.local/state/pickle-air-cli')
    s=p.add_subparsers(dest='command',required=True)
    s.add_parser('doctor')
    r=s.add_parser('read');r.add_argument('--agent',required=True);r.add_argument('--pages',type=int,default=0);r.add_argument('--copy-messages',type=int,default=0)
    a=s.add_parser('ask');a.add_argument('--agent',required=True);a.add_argument('--message',required=True)
    a.add_argument('--attach',action='append',default=[]);a.add_argument('--download-dir');a.add_argument('--download-name')
    a.add_argument('--timeout',type=int,default=120);a.add_argument('--request-id');a.add_argument('--quiet',type=int,default=0)
    d=s.add_parser('download');d.add_argument('--agent',required=True);d.add_argument('--name',required=True);d.add_argument('--output-dir',required=True)
    g=s.add_parser('download-image');g.add_argument('--agent',required=True);g.add_argument('--output-dir',required=True)
    u=s.add_parser('usage');u.add_argument('--agent',required=True)
    return p

def dispatch(args):
    try:
        adb=ADB(args.port)
        if args.command=='doctor':
            facts={'version':'0.1.0','endpoint':f'127.0.0.1:{args.port}','python':sys.version.split()[0], 'swift':bool(shutil.which('swift')), 'changes_settings':False,'creates_adb_keys':False}
            facts['adb']=adb.shell('echo CONNECTED; getprop ro.build.version.release').strip().splitlines()
            if sys.platform=='darwin' and facts['swift']:
                check=subprocess.run(['swift',str(Path(__file__).with_name('clipboard.swift')),'--check'],capture_output=True,text=True,timeout=60)
                facts['unicode_permission']=check.stdout.strip()
            output(facts);return 0
        ui=Pickle(adb,args.agent)
        if args.command=='read':
            if not 0<=args.pages<=30:raise CLIError('Pages must be 0–30')
            if not 0<=args.copy_messages<=20:raise CLIError('Copy count must be 0–20')
            last={'full_agent_messages':ui.copy_bubbles(limit=args.copy_messages)['messages']} if args.copy_messages else {}
            if not args.pages:output({**last,'messages':ui.read(),'scope':'currently visible chat only'});return 0
            c=ui.collect(args.pages)
            output({**last,'messages':[{'text':t,'from':'me' if out else 'agent'} for t,out in c['items']],'scope':'scrolled back up to %d pages, oldest first'%args.pages,
                    'reached_top_of_history':c['top'],'possible_gap':c['gap'],'pages_scrolled':c['scrolled']});return 0
        if args.command=='download':output(ui.download(args.name,args.output_dir));return 0
        if args.command=='download-image':output(ui.download_image(args.output_dir));return 0
        if args.command=='usage':output(ui.usage());return 0
        if not 10<=args.timeout<=600:raise CLIError('Timeout must be 10–600 seconds')
        if not 0<=args.quiet<=300:raise CLIError('Quiet time must be 0–300 seconds')
        if not args.message or '\0' in args.message or len(args.message)>12000:raise CLIError('Message must contain 1–12000 characters, without NUL')
        if len(args.attach)>4:raise CLIError('Maximum 4 attachments per operation')
        if args.download_name and not args.download_dir:raise CLIError('--download-name requires --download-dir')
        if args.download_name and not SAFE_DL.fullmatch(args.download_name):raise CLIError('Unsupported download filename')
        attachments=[];staged=[]
        for f in args.attach:
            p=Path(f).expanduser()
            if p.is_symlink() or not p.is_file() or p.stat().st_size>8*1024*1024:raise CLIError('Invalid attachment (regular files up to 8 MiB only)')
            if p.suffix.lower() not in {'.png','.jpg','.jpeg','.txt','.pdf','.csv'}:raise CLIError('Attachment extension not in supported allowlist')
            with p.open("rb") as f:data=f.read(8*1024*1024+1)
            if len(data)>8*1024*1024:raise CLIError("Attachment grew beyond size limit")
            attachments.append(digest(data));staged.append((p,data))
        from .core import SAFE_ASCII
        if not SAFE_ASCII.fullmatch(args.message):
            pass  # Local change: non-ASCII text now goes through Android's clipboard service; the Swift helper is only a fallback.
        fingerprint=digest(json.dumps([args.agent,args.message,attachments],ensure_ascii=False).encode())
        request_id=args.request_id or fingerprint[:24]
        journal=Journal(args.state_dir,request_id,fingerprint)
        with journal.lock():
            ui.empty();before_messages=ui.read();baseline={m['text'] for m in before_messages}
            prior_outgoing=sum(m['text']==args.message and ui.mine(m) for m in before_messages)
            folder='pickle-cli-'+uuid.uuid4().hex[:16]
            names=ui.upload(staged,folder) if args.attach else []
            for name in names:ui.attach(folder,name)
            # Local change: a large image's chip can appear a few seconds after the picker closes; wait for it.
            def chips():
                nodes=ui.verify(ui.snapshot())
                if sum(n.get('content-desc') in ['첨부 삭제','Remove attachment'] for n in nodes)!=len(names):raise CLIError('Attachment chip count differs; nothing sent')
                return nodes
            nodes=ui.until(chips,20) if names else ui.verify(ui.snapshot())
            ui.text(args.message)
            nodes=ui.verify(ui.snapshot())
            if ui.field(nodes).get('text')!=args.message:raise CLIError('Draft changed before send')
            if sum(n.get('content-desc') in ['첨부 삭제','Remove attachment'] for n in nodes)!=len(names):raise CLIError('Attachments changed before send')
            button=ui.choose(nodes,'content-desc',LABELS['send'],ui.field(nodes).get('package'))
            if button.get('enabled')!='true':raise CLIError('Send button is not enabled')
            journal.set('send_attempted')  # Durable before the only send tap.
            ui.tap(button)
            deadline=time.monotonic()+args.timeout;replies=[];seen=set();last_change=time.monotonic();confirmed=False;download=None
            while time.monotonic()<deadline:
                try:messages=ui.read()
                except CLIError:time.sleep(1);continue  # Read retry only. Never Send again.
                texts=[m['text'] for m in messages]
                outgoing=sum(m['text']==args.message and ui.mine(m) for m in messages)
                if outgoing>prior_outgoing and not confirmed:
                    check=ui.verify(ui.snapshot())
                    if not ui.field(check).get('text'):
                        confirmed=True;journal.set('sent_confirmed')
                # Local fix: 'Thinking...' / 'Typing…' are progress indicators, not replies. While one is
                # on screen the agent is still working, so the quiet timer starts over.
                if any(STATUS.fullmatch(m['text']) for m in messages if not ui.mine(m)):last_change=time.monotonic()
                messages=[m for m in messages if not STATUS.fullmatch(m['text'])]
                new=[m['text'] for m in messages if m['text'] not in baseline and m['text']!=args.message and m['text'] not in names and not ui.mine(m) and m['text'] not in {'TXT','PNG','PDF','JPG','CSV'}]
                for text in reversed(new):
                    if text not in seen:replies.append(text);seen.add(text);last_change=time.monotonic()
                if args.download_dir:
                    wanted=[args.download_name] if args.download_name else [t for t in new if SAFE_NAME.fullmatch(t) and '.' in t and t not in names]
                    wanted=[w for w in wanted if w in new]
                    if len(wanted)==1:
                        download=ui.download(wanted[0],args.download_dir);break
                    if len(wanted)>1:raise CLIError('Multiple result files; use download --name explicitly')
                elif confirmed and replies and time.monotonic()-last_change>=(args.quiet or (8 if len(replies)>1 or len(replies[0])>80 else 20)):break  # a lone short line is often just an acknowledgement
                time.sleep(1)
            # Local addition: the polled list only holds text that was on screen. Scroll back to the
            # sent message and rebuild the whole reply in reading order. Never sends anything.
            if confirmed and args.attach:
                try:adb.shell('rm -r '+shlex.quote('/sdcard/Download/'+folder))  # uploaded copies are no longer needed
                except CLIError:pass
            complete=False;final=None;bubbles=[];files=[];images=0
            if replies:
                # Preferred: the app's own Copy on the newest agent message gives the full original text.
                try:
                    c=ui.copy_bubbles(args.message)
                    if c['messages']:files=c['files'];images=c['images'];bubbles=c['messages'];final='\n\n'.join(bubbles);complete=c['reached']
                    # Local fix: a long reply can push the sent bubble off screen before the poll saw it.
                    # Finding it while scrolling back (and it was not in the chat before) proves the send.
                    if c['reached'] and not confirmed and prior_outgoing==0:confirmed=True;journal.set('sent_confirmed')
                except CLIError:final=None
                if final is None:
                    try:final=ui.copy_last();bubbles=[final]
                    except CLIError:final=None
            if final is None and confirmed and replies:
                try:
                    c=ui.collect(30,args.message)
                    if c['reached']:
                        i=max(j for j,it in enumerate(c['items']) if it==(args.message,True))
                        full=[t for t,out in c['items'][i+1:] if not out and t not in names and t not in {'TXT','PNG','PDF','JPG','CSV'}]
                        if full:replies=full;complete=not c['gap']
                except CLIError:pass
            state='completed' if confirmed and replies and (not args.download_dir or download) else 'sent_wait_timeout' if confirmed else 'uncertain'
            journal.set(state)
            # Local addition: one short line is often only "I'll start now"; the real answer may still come.
            body=final if final is not None else '\n'.join(replies)
            may_continue=bool(replies) and not files and not images and not download and '\n' not in body.strip() and len(body.strip())<=80
            output({'request_id':request_id,'reply_may_continue':may_continue,'state':state,'sent_confirmed':confirmed,'final_reply':final,'reply_bubbles':bubbles,'reply_files':files,'reply_images':images,'replies':replies,'replies_complete':complete,'download':download,'no_automatic_resend':True})
            return 0 if state=='completed' else 4
    except (CLIError,OSError,ValueError,subprocess.SubprocessError) as e:
        output({'error':str(e),'no_automatic_resend':True,'action':'Inspect the app and local journal before any retry.'});return 2

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='doctor':return dispatch(args)
        with EndpointLock(args.port):return dispatch(args)
    except CLIError as e:
        output({'error':str(e),'no_automatic_resend':True});return 2

if __name__=='__main__':raise SystemExit(main())

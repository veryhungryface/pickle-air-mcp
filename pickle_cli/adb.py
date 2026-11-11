"""Bounded, keyless loopback ADB transport. No daemon, authentication or discovery."""
import socket, struct, time, uuid

class CLIError(RuntimeError): pass

class ADB:
    def __init__(self, port=5555, timeout=25):
        if not 1024 <= port <= 65535: raise CLIError('Invalid local port')
        self.port, self.timeout = port, timeout

    @staticmethod
    def packet(tag,a0=0,a1=0,data=b''):
        c=int.from_bytes(tag.encode('ascii'),'little')
        return struct.pack('<6I',c,a0,a1,len(data),sum(data)&0xffffffff,c^0xffffffff)+data

    @staticmethod
    def exact(s,n,deadline):
        out=bytearray()
        while len(out)<n:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise CLIError('ADB timeout')
            s.settimeout(remaining)
            chunk=s.recv(n-len(out))
            if not chunk: raise CLIError('ADB disconnected; do not resend a message automatically')
            out.extend(chunk)
        return bytes(out)

    @classmethod
    def receive(cls,s,deadline,allow_zero=False):
        c,a,b,n,check,magic=struct.unpack('<6I',cls.exact(s,24,deadline))
        if magic!=c^0xffffffff or n>16*1024*1024: raise CLIError('Invalid ADB frame')
        data=cls.exact(s,n,deadline)
        tag=c.to_bytes(4,'little').decode('ascii')
        skip=allow_zero or (tag=='CNXN' and a>=0x01000001)
        if check!=sum(data)&0xffffffff and not (skip and check==0):raise CLIError('ADB checksum mismatch')
        return c.to_bytes(4,'little').decode('ascii'),a,b,data

    def shell(self,command):
        if '\0' in command: raise CLIError('NUL in command')
        marker='__PCLI_'+uuid.uuid4().hex+'__'
        wrapped='('+command+'); r=$?; printf "\\n'+marker+':%s\\n" "$r"'
        service=('shell:'+wrapped+'\0').encode()
        if len(service)>4000: raise CLIError('ADB command too large')
        deadline=time.monotonic()+self.timeout
        output=bytearray();remote=None
        try:
            with socket.create_connection(('127.0.0.1',self.port),timeout=5) as s:
                s.sendall(self.packet('CNXN',0x01000001,4096,b'host::\0'))
                tag,version,_,_=self.receive(s,deadline)
                if tag=='AUTH': raise CLIError('ADB requires authentication; no keys generated or used')
                if tag!='CNXN': raise CLIError('Not an ADB connection')
                s.sendall(self.packet('OPEN',1,0,service))
                while True:
                    tag,a,b,data=self.receive(s,deadline,allow_zero=version>=0x01000001)
                    if b!=1: raise CLIError('Wrong ADB stream ID')
                    if tag=='OKAY':
                        if remote is not None and a!=remote:raise CLIError('ADB remote stream changed')
                        remote=a
                    elif tag=='WRTE':
                        if remote is None or a!=remote: raise CLIError('Unexpected ADB data stream')
                        output.extend(data)
                        if len(output)>64*1024*1024: raise CLIError('ADB response too large')
                        s.sendall(self.packet('OKAY',1,a))
                    elif tag=='CLSE':
                        if remote is not None and a!=remote:raise CLIError('Wrong ADB close stream')
                        s.sendall(self.packet('CLSE',1,a))
                        if remote is None: raise CLIError('ADB shell rejected; enable local ADB manually if desired')
                        break
                    else: raise CLIError('Unexpected ADB packet: '+tag)
        except (OSError,UnicodeError) as e: raise CLIError('Local ADB connection failed: '+str(e)) from e
        text=output.decode('utf-8',errors='strict').replace('\r\n','\n')
        head,sep,tail=text.rpartition('\n'+marker+':')
        if not sep or not tail.strip().isdigit(): raise CLIError('Missing shell exit status')
        if int(tail.strip()): raise CLIError('Android command failed: '+head[-400:])
        return head

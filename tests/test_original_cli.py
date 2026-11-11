import json, socket, struct, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock
from pickle_cli.adb import ADB,CLIError
from pickle_cli.core import Journal,Pickle,SAFE_ASCII,SAFE_NAME,digest

class PacketTests(unittest.TestCase):
    def test_wire_header(self):
        p=ADB.packet('CNXN',1,2,b'abc');c,a,b,n,check,magic=struct.unpack('<6I',p[:24])
        self.assertEqual((a,b,n,check),(1,2,3,294));self.assertEqual(magic,c^0xffffffff)
    def test_bad_port(self):
        for p in [0,1023,65536]:
            with self.assertRaises(CLIError):ADB(p)
    def test_partial_receive(self):
        s=Mock();s.recv.side_effect=[b'a',b'bc']
        import time
        self.assertEqual(ADB.exact(s,3,time.monotonic()+1),b'abc')
    def test_disconnect(self):
        s=Mock();s.recv.return_value=b''
        import time
        with self.assertRaises(CLIError):ADB.exact(s,1,time.monotonic()+1)
    def test_bad_magic(self):
        p=bytearray(ADB.packet('WRTE',1,1,b'x'));p[20]^=1
        s=Mock();s.recv.side_effect=[bytes(p[:24])]
        import time
        with self.assertRaises(CLIError):ADB.receive(s,time.monotonic()+1)
    def test_invalid_zero_checksum(self):
        p=bytearray(ADB.packet('WRTE',1,1,b'x'));p[16:20]=b'\0'*4
        s=Mock();s.recv.side_effect=[bytes(p[:24]),b'x']
        import time
        with self.assertRaises(CLIError):ADB.receive(s,time.monotonic()+1)
    def test_oversized(self):
        c=int.from_bytes(b'WRTE','little');p=struct.pack('<6I',c,1,1,17*1024*1024,0,c^0xffffffff)
        s=Mock();s.recv.return_value=p
        import time
        with self.assertRaises(CLIError):ADB.receive(s,time.monotonic()+1)

class SafetyTests(unittest.TestCase):
    def test_journal_prevents_resend(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(d,'request','hash')
            with j.lock():j.set('send_attempted')
            with self.assertRaises(CLIError):
                with Journal(d,'request','hash').lock():self.fail('must not enter')
            self.assertEqual(json.loads(Path(d,'request.json').read_text())['state'],'send_attempted')
    def test_request_id_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ['../bad','x/y','', 'x'*81]:
                with self.assertRaises(CLIError):Journal(d,name,'hash')
    def test_unicode_and_shell_characters_use_clipboard(self):
        for m in ['한국어','🙂','a\nb','%s','$(uname)','a; b',"a'b",'a\\b']:
            self.assertIsNone(SAFE_ASCII.fullmatch(m),m)
        self.assertIsNotNone(SAFE_ASCII.fullmatch('Hello, CLI! Reply OK.'))
    def test_filenames(self):
        for n in ['../file','a/b','-x','한글.txt','a.txt\n']:
            self.assertIsNone(SAFE_NAME.fullmatch(n),n)
        self.assertIsNotNone(SAFE_NAME.fullmatch('test-file.txt'))
    def test_endpoint_lock_blocks_other_command(self):
        from pickle_cli.core import EndpointLock
        with tempfile.TemporaryDirectory() as d:
            from unittest.mock import patch
            with patch('pathlib.Path.home',return_value=Path(d)):
                with EndpointLock(5555):
                    with self.assertRaises(CLIError):
                        with EndpointLock(5555):self.fail('second command entered')
    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,'answer.txt');p.write_text('user data')
            with self.assertRaises(CLIError):Pickle(Mock(),'agent').download('answer.txt',d)
            self.assertEqual(p.read_text(),'user data')
    def test_no_message_content_in_journal(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(d,'example',digest('private body'.encode()))
            with j.lock():pass
            data=Path(d,'example.json').read_text()
            self.assertNotIn('private body',data)
    def test_wrong_agent_stops(self):
        import xml.etree.ElementTree as E
        nodes=[E.fromstring('<node package="com.pickle.client" text="Other" bounds="[0,0][300,200]"/>')]
        with self.assertRaises(CLIError):Pickle(Mock(),'expected').verify(nodes)
    def test_existing_draft_stops(self):
        import xml.etree.ElementTree as E
        nodes=list(E.fromstring('<root><node package="com.pickle.client" text="agent" bounds="[0,0][300,200]"/><node package="com.pickle.client" class="android.widget.EditText" text="user draft" bounds="[0,1000][500,1100]"/></root>').iter('node'))
        u=Pickle(Mock(),'agent');u.snapshot=lambda:nodes
        with self.assertRaises(CLIError):u.empty()
    def test_ambiguous_target_stops(self):
        with self.assertRaises(CLIError):Pickle(Mock(),'x').choose([{'text':'Save'},{'text':'Save'}],'text',['Save'])

if __name__=='__main__':unittest.main()

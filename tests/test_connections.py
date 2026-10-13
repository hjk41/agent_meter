import io
import json
import threading
import unittest
from unittest.mock import patch
from agent_meter.connections import codex_login, official_auth_url

class RecordingInput(io.StringIO):
    def close(self): self.saved=self.getvalue(); super().close()

class FakeProcess:
    def __init__(self,messages):
        self.stdin=RecordingInput(); self.stdout=io.StringIO(''.join(json.dumps(m)+'\n' for m in messages))
        self.killed=False
    def poll(self): return 0 if self.killed else None
    def kill(self): self.killed=True
    def wait(self,timeout=None): return 0

class ConnectionTests(unittest.TestCase):
    def test_official_oauth_ignores_other_login_and_keeps_credentials_in_client(self):
        url='https://auth.openai.com/oauth/authorize?state=example'
        process=FakeProcess([{'id':1,'result':{}}, {'id':2,'result':{'loginId':'ours','authUrl':url}},
            {'method':'account/login/completed','params':{'loginId':'other','success':True}},
            {'method':'account/login/completed','params':{'loginId':'ours','success':True}}])
        urls=[]; status=[]
        with patch('agent_meter.connections.subprocess.Popen',return_value=process) as launch:
            success,_=codex_login({'mode':'direct'},threading.Event(),urls.append,status.append,command=['official.exe'])
        self.assertTrue(success); self.assertEqual(urls,[url]); self.assertTrue(process.killed)
        sent=[json.loads(line) for line in process.stdin.saved.splitlines()]
        self.assertEqual(sent[2]['params'],{'type':'chatgpt'})
        self.assertFalse(any('token' in line.lower() for line in process.stdin.saved.splitlines()))
        self.assertNotIn('HTTPS_PROXY',launch.call_args.kwargs['env'])

    def test_rejects_untrusted_browser_urls(self):
        for url in ['http://auth.openai.com','https://auth.openai.com.evil.test','https://secret@chatgpt.com','file:///private']:
            self.assertFalse(official_auth_url(url))
        process=FakeProcess([{'id':1,'result':{}},{'id':2,'result':{'loginId':'ours','authUrl':'https://evil.test'}}])
        urls=[]
        with patch('agent_meter.connections.subprocess.Popen',return_value=process):
            success,_=codex_login({'mode':'direct'},threading.Event(),urls.append,lambda _:None,command=['official.exe'])
        self.assertFalse(success); self.assertEqual(urls,[]); self.assertTrue(process.killed)

    def test_cancel_terminates_official_login_process(self):
        process=FakeProcess([]); cancelled=threading.Event(); cancelled.set()
        with patch('agent_meter.connections.subprocess.Popen',return_value=process):
            success,message=codex_login({'mode':'custom','url':'http://localhost:7890'},cancelled,
                lambda _:self.fail('should not open browser'),lambda _:None,command=['official.exe'])
        self.assertFalse(success); self.assertIn('取消',message); self.assertTrue(process.killed)

if __name__=='__main__': unittest.main()

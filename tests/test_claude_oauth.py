import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
from agent_meter.claude_oauth import (begin_login, authorization_code, OAuthError,
    ClaudeClient, CredentialStore, TOKEN_URL, USAGE_URL, safe_login_error)
from agent_meter.providers import parse_claude_oauth, collect
from agent_meter.storage import normalize

class Response:
    def __init__(self,data,status=200,headers=None): self.data=data; self.status_code=status; self.headers=headers or {}
    def json(self): return self.data

class Session:
    def __init__(self,posts=(),gets=()): self.posts=list(posts); self.gets=list(gets); self.calls=[]
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def post(self,url,**kwargs):
        self.calls.append(('post',url,kwargs))
        response=self.posts.pop(0)
        return response() if callable(response) else response
    def get(self,url,**kwargs):
        self.calls.append(('get',url,kwargs)); return self.gets.pop(0)

def tokens(access='ACCESS-PRIVATE',refresh='REFRESH-PRIVATE'):
    return {'access_token':access,'refresh_token':refresh,'expires_in':3600,'scope':'user:profile user:inference'}

class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.client=ClaudeClient(self.tmp.name)

    def test_pkce_and_callback_validation(self):
        flow=begin_login(); other=begin_login(); p=urlparse(flow.url); query=parse_qs(p.query)
        self.assertEqual(p.hostname,'claude.ai'); self.assertNotEqual(flow.state,other.state)
        expected=base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'],[expected]); self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertEqual(authorization_code('ok#'+flow.state,flow),'ok')
        self.assertEqual(authorization_code('https://platform.claude.com/oauth/code/callback?code=ok&state='+flow.state,flow),'ok')
        for bad in ['ok#wrong','https://evil.test/?code=ok','https://platform.claude.com/oauth/code/callback?code=ok&state=wrong','']:
            with self.assertRaises(OAuthError): authorization_code(bad,flow)
        flow.deadline=0
        with self.assertRaises(OAuthError): authorization_code('ok',flow)
        flow=begin_login(); flow.cancelled.set()
        with self.assertRaises(OAuthError): authorization_code('ok',flow)

    def test_dpapi_roundtrip_and_no_plaintext_on_disk(self):
        self.client.store.save({'access_token':'ACCESS-PRIVATE','refresh_token':'REFRESH-PRIVATE','expires_at':123})
        raw=self.client.store.path.read_text()
        self.assertNotIn('ACCESS-PRIVATE',raw); self.assertNotIn('REFRESH-PRIVATE',raw)
        self.assertEqual(self.client.store.load()['refresh_token'],'REFRESH-PRIVATE')
        envelope=json.loads(raw); envelope['blob']='not encrypted'
        self.client.store.path.write_text(json.dumps(envelope))
        with self.assertRaises(OAuthError): self.client.store.load()

    def test_exchange_and_query_use_selected_network_and_do_not_infer(self):
        flow=begin_login(); session=Session([Response(tokens())],[Response({'five_hour':{'utilization':25}})])
        network={'mode':'custom','url':'http://127.0.0.1:7890'}
        with patch('agent_meter.claude_oauth.session_for',return_value=session) as factory:
            self.client.exchange('AUTH-CODE#'+flow.state,flow,network)
            data=self.client.usage(network)
        self.assertEqual(data['five_hour']['utilization'],25)
        self.assertEqual([call[1] for call in session.calls],[TOKEN_URL,USAGE_URL])
        self.assertEqual(session.calls[0][2]['json']['code_verifier'],flow.verifier)
        self.assertEqual(session.calls[1][2]['headers']['anthropic-beta'],'oauth-2025-04-20')
        self.assertTrue(all(call.args==(network,) for call in factory.call_args_list))
        self.assertTrue(all(call[2]['allow_redirects'] is False for call in session.calls))
        self.assertNotIn('AUTH-CODE',self.client.store.path.read_text())

    def test_close_during_exchange_does_not_save_tokens(self):
        flow=begin_login()
        def reply(): flow.cancelled.set(); return Response(tokens())
        with patch('agent_meter.claude_oauth.session_for',return_value=Session([reply])):
            with self.assertRaises(OAuthError): self.client.exchange('code',flow,{'mode':'direct'})
        self.assertFalse(self.client.store.path.exists())

    def test_forget_during_exchange_cannot_restore_credentials(self):
        flow=begin_login()
        def reply(): self.client.forget(); return Response(tokens())
        with patch('agent_meter.claude_oauth.session_for',return_value=Session([reply])):
            with self.assertRaises(OAuthError): self.client.exchange('code',flow,{'mode':'direct'})
        self.assertFalse(self.client.store.path.exists())

    def test_expiry_refresh_rotates_tokens_and_401_retries_once(self):
        self.client.store.save({'access_token':'old','refresh_token':'refresh','expires_at':time.time()-1,'scopes':['user:profile']})
        session=Session([Response(tokens('new','rotated'))],[Response({'seven_day':{'utilization':40}})])
        with patch('agent_meter.claude_oauth.session_for',return_value=session): self.client.usage({'mode':'direct'})
        self.assertEqual(self.client.store.load()['refresh_token'],'rotated')
        self.assertEqual(session.calls[1][2]['headers']['Authorization'],'Bearer new')
        session=Session([Response(tokens('retry'))],[Response({},401),Response({'five_hour':{'utilization':1}})])
        with patch('agent_meter.claude_oauth.session_for',return_value=session): self.client.usage({'mode':'direct'})
        self.assertEqual([call[0] for call in session.calls],['get','post','get'])

    def test_rate_limit_blocks_subsequent_requests_and_auth_error_is_not_zero(self):
        self.client.store.save({'access_token':'a','refresh_token':'r','expires_at':time.time()+3600})
        session=Session(gets=[Response({},429,{'Retry-After':'300'})])
        with patch('agent_meter.claude_oauth.session_for',return_value=session):
            for _ in range(2):
                with self.assertRaises(OAuthError): self.client.usage({'mode':'direct'})
        self.assertEqual(len(session.calls),1); self.assertGreater(self.client.blocked_until,time.monotonic()+290)
        self.client.blocked_until=0
        session=Session([Response({},400)],[Response({},401)])
        with patch('agent_meter.claude_oauth.session_for',return_value=session):
            with self.assertRaises(OAuthError): self.client.usage({'mode':'direct'})
        self.assertEqual(parse_claude_oauth({}).quotas,[])

    def test_own_account_is_prioritized_and_local_login_is_never_copied(self):
        local={'access_token':'local','expires_at':time.time()+3600}
        session=Session(gets=[Response({'five_hour':{'utilization':0}})])
        with patch('agent_meter.claude_oauth.local_credentials',return_value=local), patch('agent_meter.claude_oauth.session_for',return_value=session):
            self.client.usage({'mode':'direct'})
        self.assertFalse(self.client.store.path.exists())
        self.client.store.save({'access_token':'ours','expires_at':time.time()+3600})
        session=Session(gets=[Response({})])
        with patch('agent_meter.claude_oauth.local_credentials',side_effect=AssertionError('should use explicit account')), patch('agent_meter.claude_oauth.session_for',return_value=session):
            self.client.usage({'mode':'direct'})
        self.assertEqual(session.calls[0][2]['headers']['Authorization'],'Bearer ours')

    def test_quota_iso_timestamps_unknown_fields_and_provider_error_redaction(self):
        snapshot=parse_claude_oauth({'five_hour':{'utilization':0,'resets_at':'2030-01-01T00:00:00Z'},
            'seven_day':{'utilization':99},'seven_day_opus':None,'extra_usage':{'utilization':77}})
        self.assertEqual([q.remaining for q in snapshot.quotas],[100,1])
        self.assertEqual(snapshot.primary.reset_at,1893456000)
        self.assertNotIn('secret',safe_login_error(requests.ConnectionError('secret')))
        with patch('agent_meter.claude_oauth.ClaudeClient.usage',side_effect=OAuthError('请重新登录')):
            snapshot=collect('claude',normalize({}),self.tmp.name)
        self.assertEqual(snapshot.status,'error'); self.assertEqual(snapshot.message,'请重新登录')

if __name__=='__main__': unittest.main()

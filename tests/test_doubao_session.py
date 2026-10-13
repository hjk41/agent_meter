import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
from contextlib import closing

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from agent_meter import doubao, doubao_session as session

class SessionTests(unittest.TestCase):
    def test_local_import_is_read_only_and_scoped_to_doubao_auth_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            profile=Path(directory); network=profile/'Default/Network'; network.mkdir(parents=True)
            (profile/'Local State').write_text(json.dumps({'os_crypt':{'encrypted_key':
                base64.b64encode(b'DPAPIencrypted-key').decode()}}))
            key=AESGCM.generate_key(bit_length=256); nonce=b'012345678901'
            host='.doubao.com'; value='private-test-session'
            encrypted=b'v10'+nonce+AESGCM(key).encrypt(nonce,hashlib.sha256(host.encode()).digest()+value.encode(),None)
            database=network/'Cookies'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE cookies(host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB)')
                db.executemany('INSERT INTO cookies VALUES(?,?,?,?)',[
                    (host,'sessionid','',encrypted),('.other.example','sessionid','unrelated',b''),
                    (host,'private-conversation','unrelated',b'')])
                db.commit()
            before=database.read_bytes()
            with patch.object(session,'dpapi',return_value=key):
                self.assertEqual(session.local_cookies(profile),{'sessionid':value})
            self.assertEqual(database.read_bytes(),before)
            with self.assertRaises(session.SessionError):session.decrypt_cookie(b'v20unsupported',key,host)

    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_own_session_is_encrypted_and_survives_restart_without_local_app_access(self):
        with tempfile.TemporaryDirectory() as directory:
            store=session.SessionStore(directory)
            store.save({'sessionid':'private-test-session','unrelated':'discarded'})
            self.assertNotIn('private-test-session',store.path.read_text())
            self.assertEqual(session.SessionStore(directory).load(),{'sessionid':'private-test-session'})
            with patch.object(session,'local_cookies',side_effect=session.SessionError('locked')):
                factory,response=self.http_response()
                session.read_http_quota(directory,{'mode':'direct'},factory)
                factory.return_value.__enter__.return_value.cookies.set.assert_called_once_with(
                    'sessionid','private-test-session',domain='.doubao.com',path='/')

    def http_response(self):
        response=Mock(status_code=200)
        response.json.return_value={'code':0,'private_identity':'discarded','data':{
            'current_subscription':{'display':{'short_name':'标准套餐'},'subscription_id':'discarded'},
            'window_limit_section':{'window_limit_groups':[{'feature_group_name':'','window_limits':[
                {'window_type':2,'used_percent':0,'less_than_one_percent':True,'end_time':3000000,
                 'private_identity':'discarded','exemption':{'active':False,'private_identity':'discarded'}}]}]}}}
        factory=MagicMock(); factory.return_value.__enter__.return_value.post.return_value=response
        return factory,response

    def test_request_uses_membership_selected_proxy_and_no_redirects_or_private_fields(self):
        factory,response=self.http_response(); network={'mode':'custom','url':'http://localhost:7890'}
        with patch.object(session,'local_cookies',return_value={'sessionid':'private-session'}), \
             patch.object(session.SessionStore,'save') as save:
            data=session.read_http_quota('.',network,factory)
        factory.assert_called_once_with(network)
        request=factory.return_value.__enter__.return_value.post.call_args
        self.assertEqual(request.args[0],session.QUOTA_URL)
        self.assertEqual(request.kwargs['json'],{'product_line':'membership'})
        self.assertFalse(request.kwargs['allow_redirects'])
        self.assertNotIn('private_identity',json.dumps(data))
        self.assertNotIn('private-session',json.dumps(data))
        self.assertEqual(response.encoding,'utf-8')
        self.assertEqual(doubao.parse_quota(data,1000).primary.remaining_display,'>99%')
        save.assert_called_once()

    def test_rejected_session_does_not_save_or_expose_upstream_error(self):
        factory,response=self.http_response(); response.status_code=403
        response.text='private-session and account identity'
        with patch.object(session,'local_cookies',return_value={'sessionid':'private-session'}), \
             patch.object(session.SessionStore,'save') as save:
            with self.assertRaises(session.SessionError) as error:
                session.read_http_quota('.',{'mode':'direct'},factory)
        self.assertNotIn('private-session',str(error.exception)); save.assert_not_called()
        response.json.assert_not_called()

    def test_http_success_does_not_require_desktop_renderer(self):
        factory,response=self.http_response()
        data=session.quota_fields(response.json.return_value)
        with patch.object(doubao,'read_http_quota',return_value=data), \
             patch.object(doubao,'read_quota') as desktop:
            value=doubao.collect_doubao('private-directory',{'mode':'direct'})
        self.assertEqual(value.status,'live'); self.assertIn('独立',value.source)
        desktop.assert_not_called()

if __name__=='__main__':unittest.main()

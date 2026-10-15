import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from agent_meter.devin import collect_devin, credentials_path, parse_devin, STATUS_PATH
from agent_meter.model import Quota, Snapshot, merge_snapshot
from agent_meter.providers import collect
from agent_meter.storage import normalize


class DevinTests(unittest.TestCase):
    def test_daily_weekly_are_remaining_percent_and_zero_is_valid(self):
        data={'userStatus':{'planStatus':{'dailyQuotaRemainingPercent':0,
            'weeklyQuotaRemainingPercent':71.5,'dailyQuotaResetAtUnix':'200',
            'weeklyQuotaResetAtUnix':'300','availablePromptCredits':-1,
            'overageBalanceMicros':90000000}},'planInfo':{'planName':'Pro'}}
        snapshot=parse_devin(data,100)
        self.assertEqual(snapshot.status,'live')
        self.assertEqual(snapshot.collected_at,100)
        self.assertEqual([(q.name,q.remaining,q.reset_at) for q in snapshot.quotas],
                         [('每日',0,200),('每周',71.5,300)])
        self.assertEqual(len(snapshot.quotas),2)

    def test_hidden_missing_invalid_windows_do_not_become_full_allowances(self):
        data={'user_status':{'plan_status':{'daily_quota_remaining_percent':100,
            'weekly_quota_remaining_percent':0}},'plan_info':{'hide_daily_quota':True}}
        self.assertEqual([q.name for q in parse_devin(data).quotas],['每周'])
        for invalid in (None,-1,101,'NaN',float('inf'),True,False,{},[]):
            data={'userStatus':{'planStatus':{'dailyQuotaRemainingPercent':invalid,
                                            'dailyQuotaResetAtUnix':'200'}}}
            snapshot=parse_devin(data)
            self.assertEqual(snapshot.quotas,[])
            self.assertEqual(snapshot.status,'waiting')
        self.assertEqual(parse_devin({'userStatus':{'planStatus':{
            'availablePromptCredits':-1,'acuConsumed':10,'acuLimit':100}}}).quotas,[])

    def test_native_paths_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('sys.platform','win32'),patch.dict(os.environ,{'APPDATA':directory,'AGENT_METER_DEVIN_CREDENTIALS':''}):
                self.assertEqual(credentials_path(),root/'devin/credentials.toml')
            for system in ('darwin','linux'):
                with patch('sys.platform',system),patch('pathlib.Path.home',return_value=root), \
                     patch.dict(os.environ,{'XDG_DATA_HOME':'','AGENT_METER_DEVIN_CREDENTIALS':''}):
                    self.assertEqual(credentials_path(),root/'.local/share/devin/credentials.toml')
                    with patch.dict(os.environ,{'XDG_DATA_HOME':str(root/'custom-data')}):
                        self.assertEqual(credentials_path(),root/'custom-data/devin/credentials.toml')
            with patch.dict(os.environ,{'AGENT_METER_DEVIN_CREDENTIALS':str(root/'alternate.toml')}):
                self.assertEqual(credentials_path(),root/'alternate.toml')

    def test_reloads_credentials_uses_proxy_and_only_posts_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credentials.toml'
            response=Mock(status_code=200)
            response.json.return_value={'userStatus':{'planStatus':{'weeklyQuotaRemainingPercent':45}}}
            session=Mock(); session.post.return_value=response
            network={'mode':'custom','url':'http://127.0.0.1:1080'}
            with patch('agent_meter.devin.credentials_path',return_value=path), \
                 patch('agent_meter.providers.http_session') as sessions:
                sessions.return_value.__enter__.return_value=session
                for token in ('first-test-key','second-test-key'):
                    original=f'windsurf_api_key = "{token}"\napi_server_url = "https://server.self-serve.windsurf.com"\n'
                    path.write_text(original,encoding='utf-8')
                    snapshot=collect_devin(network)
                    sessions.assert_called_with(network)
                    args,kwargs=session.post.call_args
                    self.assertEqual(args[0],'https://server.self-serve.windsurf.com'+STATUS_PATH)
                    self.assertEqual(kwargs['json']['metadata']['apiKey'],token)
                    self.assertFalse(kwargs['allow_redirects'])
                    self.assertNotIn(token,json.dumps(snapshot.to_dict()))
                    self.assertEqual(path.read_text(),original)

    def test_login_failures_and_redirects_never_expose_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credentials.toml'
            path.write_text('windsurf_api_key="secret-test-value"\napi_server_url="https://server.self-serve.windsurf.com"')
            settings=normalize({})
            with patch('agent_meter.devin.credentials_path',return_value=path), \
                 patch('agent_meter.providers.http_session') as sessions:
                response=sessions.return_value.__enter__.return_value.post.return_value
                response.status_code=401
                snapshot=collect('devin',settings,directory)
                self.assertEqual(snapshot.status,'error')
                self.assertNotIn('secret-test-value',json.dumps(snapshot.to_dict()))
                response.status_code=503
                response.raise_for_status.side_effect=requests.HTTPError('secret-test-value')
                failed=collect('devin',settings,directory)
                self.assertEqual(failed.status,'error')
                self.assertNotIn('secret-test-value',json.dumps(failed.to_dict()))
                old=Snapshot('devin','live',collected_at=100,quotas=[Quota('每周',45)])
                preserved=merge_snapshot(old,failed)
                self.assertEqual(preserved.status,'stale')
                self.assertEqual(preserved.collected_at,100)
                self.assertEqual(preserved.primary.remaining,45)
            for server in ('http://example.test','https://name:secret@example.test','https://example.test/path'):
                path.write_text(f'windsurf_api_key="secret-test-value"\napi_server_url="{server}"')
                with patch('agent_meter.devin.credentials_path',return_value=path), \
                     patch('agent_meter.providers.http_session') as sessions:
                    self.assertEqual(collect('devin',settings,directory).status,'error')
                    sessions.assert_not_called()


if __name__=='__main__': unittest.main()

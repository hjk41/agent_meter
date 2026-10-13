import copy
import json
import unittest
from unittest.mock import Mock, patch
from agent_meter import doubao
from agent_meter.model import Snapshot, effective_status, merge_snapshot, percent
from agent_meter.providers import collect

TARGET = {'type':'page', 'url':'doubao://doubao-chat/chat?test=1',
          'webSocketDebuggerUrl':'ws://127.0.0.1:9225/devtools/page/test'}
QUOTA = {'code':0, 'subscription':'标准套餐', 'groups':[{'name':'', 'windows':[
    {'window_type':1, 'used_percent':1, 'less_than_one_percent':False, 'end_time':2000000},
    {'window_type':2, 'used_percent':0, 'less_than_one_percent':True, 'end_time':3000000}]}]}

class DoubaoTests(unittest.TestCase):
    def test_windows_precision_milliseconds_and_cache(self):
        snapshot = doubao.parse_quota(QUOTA, 1000)
        self.assertEqual(snapshot.status, 'live')
        self.assertEqual([percent(q) for q in snapshot.quotas], ['99%', '>99%'])
        self.assertEqual([q.reset_at for q in snapshot.quotas], [2000, 3000])
        restored = Snapshot.from_dict(snapshot.to_dict())
        self.assertEqual(percent(restored.quotas[1]), '>99%')
        self.assertEqual(effective_status(restored, 2000), 'stale')
        old_cache = snapshot.to_dict()
        for q in old_cache['quotas']: q.pop('remaining_display')
        self.assertEqual(Snapshot.from_dict(old_cache).primary.remaining, 99)
        failed = merge_snapshot(snapshot, Snapshot('doubao', 'waiting', 'closed'))
        self.assertEqual(failed.collected_at, 1000)
        self.assertEqual(failed.status, 'stale')

    def test_missing_bad_and_exempt_values_are_not_zero_usage(self):
        data = copy.deepcopy(QUOTA)
        windows = data['groups'][0]['windows']
        windows[:] = [{'window_type':1, 'used_percent':v} for v in (None, True, 'NaN', -1, 101)]
        self.assertEqual(doubao.parse_quota(data).quotas, [])
        windows.append({'window_type':1, 'exemption':{'active':True, 'end_time':2000000}})
        snapshot = doubao.parse_quota(data, 1000)
        self.assertEqual(percent(snapshot.primary), '不限量')
        self.assertEqual(snapshot.primary.reset_at, 2000)
        self.assertEqual(doubao.parse_quota(data, 2001).quotas, [])
        self.assertEqual(doubao.parse_quota({'code':401}).status, 'waiting')
        # Exact zero without the server's precision flag remains exact 100%.
        data = copy.deepcopy(QUOTA); data['groups'][0]['windows'][1]['less_than_one_percent'] = False
        self.assertEqual(percent(doubao.parse_quota(data).quotas[1]), '100%')

    def test_target_rejects_other_apps_remote_hosts_and_credential_urls(self):
        self.assertTrue(doubao.valid_target(TARGET))
        for update in [
            {'type':'other'}, {'url':'https://www.doubao.com/chat/'},
            {'url':'doubao://another-app/chat'},
            {'webSocketDebuggerUrl':'ws://example.com:9225/devtools/page/test'},
            {'webSocketDebuggerUrl':'ws://127.0.0.1:9226/devtools/page/test'},
            {'webSocketDebuggerUrl':'ws://user:secret@127.0.0.1:9225/devtools/page/test'},
            {'webSocketDebuggerUrl':'ws://127.0.0.1:9225/devtools/page/test?token=secret'},
            {'webSocketDebuggerUrl':'ws://127.0.0.1:bad/devtools/page/test'}]:
            self.assertFalse(doubao.valid_target({**TARGET, **update}))

    def test_local_discovery_bypasses_proxy_and_ambiguity_fails_closed(self):
        session = Mock(); session.get.return_value.json.return_value = [TARGET]
        with patch.object(doubao.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            self.assertEqual(doubao.quota_target(), TARGET['webSocketDebuggerUrl'])
            self.assertFalse(session.trust_env)
            self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
            session.get.return_value.json.return_value = [TARGET, TARGET]
            with self.assertRaises(doubao.DoubaoError): doubao.quota_target()

    def test_cdp_only_evaluates_quota_and_drops_private_error_details(self):
        connection = Mock()
        connection.recv.side_effect = [json.dumps({'method':'event'}),
            json.dumps({'id':1, 'result':{'result':{'value':QUOTA}}})]
        with patch.object(doubao, 'quota_target', return_value=TARGET['webSocketDebuggerUrl']), \
             patch.object(doubao.websocket, 'create_connection', return_value=connection) as factory:
            self.assertEqual(doubao.read_quota(), QUOTA)
            self.assertEqual(factory.call_args.kwargs['http_no_proxy'], ['127.0.0.1', 'localhost'])
            self.assertEqual(factory.call_args.kwargs['redirect_limit'], 0)
            sent = json.loads(connection.send.call_args.args[0])
            self.assertEqual(sent['method'], 'Runtime.evaluate')
            self.assertIn("AGWGetSubscriptionQuotaSummary({product_line:'membership'})", sent['params']['expression'])
            connection.close.assert_called_once()
            connection.reset_mock()
            connection.recv.side_effect = [json.dumps({'id':1, 'result':{
                'exceptionDetails':{'text':'private cookie and account identity'}}})]
            with self.assertRaises(doubao.DoubaoError) as error: doubao.read_quota()
            self.assertNotIn('private', str(error.exception))
            connection.close.assert_called_once()

    def test_launcher_never_restarts_running_app_and_sets_selected_route(self):
        with patch.object(doubao, 'quota_target', side_effect=doubao.DoubaoError('closed')), \
             patch.object(doubao, 'app_running', return_value=True), \
             patch.object(doubao.subprocess, 'Popen') as launch:
            with self.assertRaises(doubao.DoubaoError): doubao.start_connection({'mode':'direct'})
            launch.assert_not_called()
        with patch.object(doubao, 'quota_target', side_effect=doubao.DoubaoError('closed')), \
             patch.object(doubao, 'app_running', return_value=False), \
             patch.object(doubao, 'installed_app', return_value='Doubao.exe'), \
             patch.object(doubao.subprocess, 'Popen') as launch:
            doubao.start_connection({'mode':'custom','url':'socks5h://localhost:1080'})
            args = launch.call_args.args[0]
            self.assertIn('--proxy-server=socks5://localhost:1080', args)
            self.assertIn('--remote-debugging-address=127.0.0.1', args)
            doubao.start_connection({'mode':'direct'})
            self.assertIn('--no-proxy-server', launch.call_args.args[0])
        with patch.object(doubao, 'quota_target', return_value='already connected'), \
             patch.object(doubao.subprocess, 'Popen') as launch:
            doubao.start_connection({'mode':'direct'})
            launch.assert_not_called()

    def test_live_data_wins_over_manual_and_manual_fallback_stays_labelled(self):
        settings = {'manual':{'doubao':{'remaining':40}}}
        with patch.object(doubao, 'read_http_quota', side_effect=doubao.SessionError('unavailable')), \
             patch.object(doubao, 'read_quota', return_value=QUOTA):
            self.assertEqual(collect('doubao', settings, '.').status, 'live')
        with patch.object(doubao, 'read_http_quota', side_effect=doubao.SessionError('unavailable')), \
             patch.object(doubao, 'read_quota', side_effect=doubao.DoubaoError('closed')):
            self.assertEqual(collect('doubao', settings, '.').status, 'manual')
            self.assertEqual(collect('doubao', {}, '.').quotas, [])

if __name__ == '__main__': unittest.main()

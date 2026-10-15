import copy
import json
import os
import tempfile
import shutil
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch
from agent_meter.model import PROVIDERS, Quota, Snapshot, effective_status, merge_snapshot
from agent_meter.providers import codex_command, parse_codex, parse_cursor, parse_claude, proxy_environment
from agent_meter.storage import Store, validate_proxy, normalize, provider_network
from agent_meter.claude_bridge import install_bridge, uninstall_bridge

class QuotaTests(unittest.TestCase):
    def test_codex_desktop_binary_is_found_without_path_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp)/'OpenAI/Codex/bin/version/codex.exe'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {'LOCALAPPDATA':tmp, 'APPDATA':tmp}, clear=True), \
                 patch('agent_meter.providers.sys.platform', 'win32'), \
                 patch('agent_meter.providers.shutil.which', return_value=None):
                self.assertEqual(codex_command(), [str(binary)])

    def test_dynamic_codex_buckets_and_missing_values(self):
        data={'rateLimits':{'primary':{'usedPercent':99}},'rateLimitsByLimitId':{
            'other':{'limitName':'Review','primary':{'usedPercent':12,'windowDurationMins':60}},
            'codex':{'primary':{'usedPercent':0,'windowDurationMins':10080,'resetsAt':200},'secondary':{'usedPercent':None}}}}
        s=parse_codex(data,100)
        self.assertEqual([q.remaining for q in s.quotas],[100,88])
        self.assertEqual(s.quotas[0].name,'7 天')
        self.assertEqual(parse_codex({'rateLimits':None}).quotas,[])
        self.assertEqual(Quota.from_used('missing',float('nan')),None)

    def test_cursor_pools_do_not_mix_bonus_or_on_demand(self):
        s=parse_cursor({'billingCycleEnd':'200000','planUsage':{'apiPercentUsed':94.524,'autoPercentUsed':0,'limit':40000,'includedSpend':40000,'bonusSpend':999999},'spendLimitUsage':{'individualUsed':100000}},100)
        self.assertAlmostEqual(s.quotas[0].remaining,5.476)
        self.assertEqual(s.quotas[1].remaining,100)
        self.assertEqual(s.quotas[0].reset_at,200)
        grant=parse_cursor({'activeGrants':[{'totalCents':'100','remainingCents':'0','expiresAtMs':'200000'}]})
        self.assertEqual(grant.primary.remaining,0)
        self.assertEqual(parse_cursor({'planUsage':{'totalSpend':500}}).quotas,[])

    def test_claude_no_context_window_or_cost_inferred(self):
        s=parse_claude({'collected_at':100,'rate_limits':{'five_hour':{'used_percentage':0,'resets_at':200},'seven_day':None},'context_window':{'used_percentage':99},'cost':{'total_cost_usd':10}})
        self.assertEqual([q.remaining for q in s.quotas],[100])
        self.assertEqual(parse_claude({'context_window':{'used_percentage':99}}).quotas,[])

    def test_old_readings_keep_source_timestamp_and_expired_windows(self):
        old=Snapshot('codex','live','connected','official',100,[Quota('window',50,200)])
        merged=merge_snapshot(old,Snapshot('codex','error','timeout'))
        self.assertEqual(merged.collected_at,100); self.assertEqual(merged.status,'stale')
        self.assertEqual(effective_status(old,200),'stale')
        self.assertEqual(effective_status(old,150),'live')
        idle=Snapshot('claude','live',collected_at=100,quotas=[Quota('window',50)])
        self.assertEqual(effective_status(idle,281,refresh_seconds=60),'stale')
        self.assertEqual(effective_status(idle,400),'live')
        self.assertEqual(effective_status(idle,461),'stale')
        self.assertEqual(effective_status(idle,700,refresh_seconds=600),'live')

    def test_preferences_and_proxy_validation(self):
        self.assertEqual(normalize({})['refresh_seconds'],300)
        self.assertEqual(normalize({'refresh_seconds':120})['refresh_seconds'],120)
        config=normalize({'order':['codex','codex','new'],'mini_ids':[],'opacity':999})
        self.assertEqual(len(config['order']),len(PROVIDERS)); self.assertEqual(config['mini_ids'],['codex'])
        self.assertEqual(config['opacity'],100)
        config=normalize({'enabled_agents':['cursor','codex'],'mini_ids':['claude','cursor']})
        self.assertEqual(config['mini_ids'],['cursor'])
        self.assertEqual(set(normalize({})['enabled_agents']),set(PROVIDERS))
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp); store.settings['order']=['cursor','doubao','codex','claude']; store.settings['mini_ids']=['cursor','claude']; store.save()
            self.assertEqual(Store(tmp).settings['order'],store.settings['order'])
        for url in ['http://127.0.0.1:7890','socks5h://localhost:1080']: self.assertEqual(validate_proxy(url),url)
        for url in ['file:///abc','http://user:secret@host:80','http://localhost:0','https://host:443/path']:
            with self.assertRaises(ValueError): validate_proxy(url)
        env=proxy_environment({'mode':'custom','url':'http://127.0.0.1:7890'})
        self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:7890')
        direct=proxy_environment({'mode':'direct'})
        self.assertFalse(any(k.lower() in ('https_proxy','http_proxy','all_proxy') for k in direct))

    def test_bridge_install_preserves_settings_and_restores_only_own_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'data'; path=Path(tmp)/'settings.json'
            original={'permissions':{'allow':['read']},'statusLine':{'type':'command','command':'node original.cjs'}}
            path.write_text(json.dumps(original),encoding='utf-8')
            install_bridge(directory,path)
            modified=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(modified['permissions'],original['permissions'])
            install_bridge(directory,path)  # Idempotent, keeps first backup.
            uninstall_bridge(directory,path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),original)

    def test_shared_proxy_migration_and_routes(self):
        old={'network':{'cursor':{'mode':'custom','url':'http://localhost:7890'},
                        'codex':{'mode':'custom','url':'http://localhost:7890'},'claude':{'mode':'direct'}}}
        config=normalize(old)
        self.assertEqual(config['network'],{'mode':'custom','url':'http://localhost:7890','agents':['cursor','codex']})
        self.assertEqual(provider_network(config,'cursor'),{'mode':'custom','url':'http://localhost:7890'})
        self.assertEqual(provider_network(config,'claude')['mode'],'direct')
        config['network']['agents']=['claude','doubao']; config=normalize(config)
        self.assertEqual(provider_network(config,'codex')['mode'],'direct')
        self.assertEqual(provider_network(config,'claude')['url'],'http://localhost:7890')
        config['network']['agents']=[]; self.assertEqual(normalize(config)['network']['agents'],[])

    def test_bridge_executes_preserved_statusline_and_only_writes_quotas(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'data'; path=Path(tmp)/'settings.json'
            previous=Path(tmp)/'previous.cjs'
            previous.write_text("process.stdin.resume(); process.stdin.on('end',()=>console.log('original-status'));",encoding='utf-8')
            node=shutil.which('node')
            path.write_text(json.dumps({'statusLine':{'type':'command','command':f'"{Path(node).as_posix()}" "{previous.as_posix()}"'}}),encoding='utf-8')
            install_bridge(directory,path)
            raw={'model':{'display_name':'Claude'},'rate_limits':{'five_hour':{'used_percentage':25,'resets_at':200},'seven_day':None},'transcript_path':'private','token':'do-not-copy'}
            result=subprocess.run([node,str(directory/'claude-statusline.cjs')],input=json.dumps(raw),text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0)
            self.assertEqual(result.stdout.strip(),'original-status')
            captured=json.loads((directory/'claude-quota.json').read_text())
            self.assertEqual(set(captured),{'rate_limits','collected_at'})
            self.assertEqual(captured['rate_limits'],{'five_hour':{'used_percentage':25,'resets_at':200}})
            uninstall_bridge(directory,path)

if __name__=='__main__': unittest.main()

import copy
import json
import os
from pathlib import Path
import tempfile
import sys
from urllib.parse import urlparse
from .model import PROVIDERS, Snapshot, DEFAULT_REFRESH_SECONDS

def data_dir():
    override = os.environ.get('AGENT_METER_DATA_DIR')
    if override: return Path(override)
    if sys.platform == 'darwin':
        return Path.home()/'Library/Application Support/AgentMeter'
    return Path(os.environ.get('LOCALAPPDATA', Path.home()))/'AgentMeter'

DEFAULTS = {'order': ['doubao', 'codex', 'claude', 'cursor', 'devin'], 'mini_ids': ['claude', 'cursor'],
            'enabled_agents': list(PROVIDERS),
            'mini': False, 'layout': 'compact', 'opacity': 90, 'pinned': True,
            'theme': 'system', 'refresh_seconds': DEFAULT_REFRESH_SECONDS, 'position': None,
            'notifications': True, 'warning_threshold': 20,
            'network': {'mode': 'system', 'url': '', 'agents': ['cursor','codex','claude','devin']},
            'manual': {}}

def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return copy.deepcopy(default)

def normalize(data):
    result = copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        return result
    result.update({k:v for k,v in data.items() if k in DEFAULTS})
    def ids(value):
        return list(dict.fromkeys(p for p in value if p in PROVIDERS)) if isinstance(value, list) else []
    order = ids(result['order'])
    result['order'] = order+[p for p in PROVIDERS if p not in order]
    result['enabled_agents'] = ids(result['enabled_agents']) or [result['order'][0]]
    result['mini_ids'] = [p for p in ids(result['mini_ids']) if p in result['enabled_agents']][:3] or [
        next(p for p in result['order'] if p in result['enabled_agents'])]
    result['layout'] = result['layout'] if result['layout'] in ('compact', 'dock') else 'compact'
    result['theme'] = result['theme'] if result['theme'] in ('system','light','dark') else 'system'
    for name, low, high in [('opacity',55,100), ('refresh_seconds',30,600), ('warning_threshold',1,50)]:
        try: result[name] = max(low,min(high,int(result[name])))
        except (TypeError,ValueError): result[name] = DEFAULTS[name]
    net = result['network'] if isinstance(result['network'],dict) else {}
    if any(p in net for p in PROVIDERS):
        # Migrate the former independent settings to a single shared address.
        custom = [v for p,v in net.items() if p in PROVIDERS and isinstance(v,dict)
                  and v.get('mode') == 'custom' and v.get('url')]
        if custom:
            url = str(custom[0]['url'])
            net = {'mode':'custom','url':url,'agents':[p for p,v in net.items()
                if p in PROVIDERS and isinstance(v,dict) and v.get('mode')=='custom' and v.get('url')==url]}
        else:
            net = {'mode':'system','url':'','agents':[p for p in ('cursor','codex','claude')
                if not isinstance(net.get(p),dict) or net[p].get('mode')!='direct']}
    result['network'] = {'mode':net.get('mode') if net.get('mode') in ('system','custom') else 'system',
                         'url':str(net.get('url','')), 'agents':ids(net.get('agents',DEFAULTS['network']['agents']))}
    if not isinstance(result['manual'],dict): result['manual'] = {}
    return result

def provider_network(settings, provider):
    """Unchecked agents bypass environment/system proxies as well."""
    net = settings['network']
    if provider not in net['agents']:
        return {'mode':'direct','url':''}
    return {'mode':net['mode'],'url':net['url'] if net['mode']=='custom' else ''}

def validate_proxy(url):
    p = urlparse(url)
    if p.scheme not in ('http','https','socks5','socks5h') or not p.hostname:
        raise ValueError('代理地址格式： http://127.0.0.1:7890 或 socks5://127.0.0.1:1080')
    if p.username or p.password:
        raise ValueError('暂不支持在代理地址中保存用户名或密码。')
    try:
        if not p.port or not 1 <= p.port <= 65535: raise ValueError()
    except ValueError:
        raise ValueError('请填写有效的代理端口。')
    if p.path not in ('','/') or p.query or p.fragment:
        raise ValueError('代理地址不应包含路径、查询或片段。')
    return url

class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or data_dir())
        self.settings = normalize(read_json(self.directory/'settings.json',{}))

    def save(self):
        self.settings = normalize(self.settings)
        atomic_json(self.directory/'settings.json', self.settings)

    def load_cache(self):
        result = {}
        cache = read_json(self.directory/'quota-cache.json',{})
        if not isinstance(cache,dict): return result
        for p, value in cache.items():
            if p not in PROVIDERS: continue
            try:
                s = Snapshot.from_dict(value)
                if s.status == 'live': s.status = 'stale'; s.message = '上次读数 · 正在刷新'
                result[p] = s
            except (ValueError,TypeError,KeyError): pass
        return result

    def save_cache(self, snapshots):
        atomic_json(self.directory/'quota-cache.json',{p:s.to_dict() for p,s in snapshots.items()})

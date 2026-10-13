"""Read-only collectors using platform-native client and credential locations."""
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import requests
from .model import Quota, Snapshot, number
from . import __version__
from .storage import read_json, validate_proxy, provider_network

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def proxy_environment(network):
    env = os.environ.copy()
    mode = network.get('mode','system')
    if mode != 'system':
        for key in list(env):
            if key.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy'):
                del env[key]
    if mode == 'custom':
        url = validate_proxy(network.get('url',''))
        env.update(HTTP_PROXY=url, HTTPS_PROXY=url, ALL_PROXY=url)
    elif mode == 'system':
        proxies = requests.utils.getproxies()
        for scheme in ('http','https'):
            if proxies.get(scheme): env[scheme.upper()+'_PROXY'] = proxies[scheme]
    return env

def http_session(network):
    session = requests.Session()
    mode = network.get('mode','system')
    session.trust_env = mode == 'system'
    if mode == 'custom':
        url = validate_proxy(network.get('url',''))
        session.proxies = {'http':url,'https':url}
    return session

def codex_command():
    explicit = os.environ.get('AGENT_METER_CODEX_EXE')
    if explicit and Path(explicit).is_file(): return [explicit]
    found = shutil.which('codex.exe' if sys.platform == 'win32' else 'codex')
    if found: return [found]
    if sys.platform == 'darwin':
        from .platform_support import mac_app
        candidates = [Path.home()/'.local/bin/codex',
            Path.home()/'.codex/packages/standalone/current/bin/codex',
            Path('/opt/homebrew/bin/codex'), Path('/usr/local/bin/codex')]
        for name in ('Codex', 'ChatGPT'):
            app = mac_app(name)
            if app:
                candidates += [app/'Contents/Resources/codex',
                               app/'Contents/Resources/bin/codex']
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK): return [str(path)]
        raise FileNotFoundError('Codex CLI')
    # The ChatGPT desktop app installs its own Codex binary outside PATH.
    # Its versioned bin directory changes when the app updates.
    local_appdata = Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()/'AppData/Local'
    desktop_bin = local_appdata/'OpenAI/Codex/bin'
    desktop_exes = [path for path in desktop_bin.glob('*/codex.exe') if path.is_file()]
    if desktop_exes:
        return [str(max(desktop_exes, key=lambda path: path.stat().st_mtime))]
    appdata = Path(os.environ['APPDATA']) if os.environ.get('APPDATA') else Path.home()/'AppData/Roaming'
    base = appdata/'npm/node_modules/@openai/codex'
    executables = list(base.glob('**/codex.exe'))
    if executables: return [str(executables[0])]
    js = base/'bin/codex.js'
    node = shutil.which('node.exe')
    if node and js.is_file(): return [node, str(js)]
    raise FileNotFoundError('Codex CLI')

def parse_codex(data, now=None):
    windows = []
    buckets = data.get('rateLimitsByLimitId')
    if not isinstance(buckets,dict) or not buckets:
        buckets = {'codex':data.get('rateLimits') or {}}
    for ident,bucket in sorted(buckets.items(),key=lambda item:item[0]!='codex'):
        if not isinstance(bucket,dict): continue
        label = bucket.get('limitName') or ('' if ident == 'codex' else ident)
        for key in ('primary','secondary'):
            w = bucket.get(key)
            if not isinstance(w,dict): continue
            mins = number(w.get('windowDurationMins'))
            period = f'{mins/1440:g} 天' if mins and mins>=1440 else f'{mins/60:g} 小时' if mins and mins>=60 else f'{mins:g} 分钟' if mins else ('主额度' if key=='primary' else '其他额度')
            quota = Quota.from_used((label+' · ' if label else '')+period, w.get('usedPercent'),w.get('resetsAt'))
            if quota: windows.append(quota)
    return Snapshot('codex','live' if windows else 'waiting','已连接' if windows else '账号未返回套餐额度',
                    'Codex 官方 App Server',time.time() if now is None else now,windows)

def collect_codex(network):
    # A fresh short-lived process picks up CLI login/token refresh and changed
    # proxy settings. No thread or inference turn is started.
    process = subprocess.Popen(codex_command()+['app-server'],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
        env=proxy_environment(network),creationflags=CREATE_NO_WINDOW)
    inbox = queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                try: inbox.put(json.loads(line))
                except ValueError: continue
        finally: inbox.put(None)
    worker = threading.Thread(target=reader,daemon=True)
    worker.start()
    deadline = time.monotonic()+25
    def send(method, ident=None, params=None):
        msg={'method':method}
        if ident is not None: msg['id']=ident
        if params is not None: msg['params']=params
        process.stdin.write(json.dumps(msg)+'\n'); process.stdin.flush()
    def wait(ident):
        while True:
            remaining = deadline-time.monotonic()
            if remaining<=0: raise TimeoutError()
            try: response=inbox.get(timeout=remaining)
            except queue.Empty: raise TimeoutError()
            if response is None: raise RuntimeError('CLI stopped')
            if response.get('id') != ident: continue
            if 'error' in response:
                error=response.get('error') or {}
                message=str(error.get('message','')).lower()
                if any(word in message for word in ('401','403','unauthorized','not authenticated','not logged','sign in')):
                    raise PermissionError('Account quota unavailable')
                raise requests.ConnectionError('Quota service unavailable')
            return response.get('result',{})
    try:
        send('initialize',1,{'clientInfo':{'name':'agent_meter','title':'Agent Meter','version':__version__}})
        wait(1); send('initialized')
        send('account/rateLimits/read',2)
        return parse_codex(wait(2))
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=5)
        worker.join(timeout=2)
        for stream in (process.stdin,process.stdout):
            if stream: stream.close()

def cursor_database():
    from .platform_support import application_support
    return application_support()/'Cursor/User/globalStorage/state.vscdb'

def parse_cursor(data, now=None):
    windows = []
    plan=data.get('planUsage') or {}
    cycle_end=number(data.get('billingCycleEnd'))
    # The client's protocol expresses this timestamp in milliseconds.
    reset=cycle_end/1000 if cycle_end else None
    for key,name in [('apiPercentUsed','其他模型 · 月度'),('autoPercentUsed','Cursor / Auto 模型 · 月度')]:
        q=Quota.from_used(name,plan.get(key),reset)
        if q: windows.append(q)
    if not windows and plan:
        q=Quota.from_used('套餐额度 · 月度',plan.get('totalPercentUsed'),reset)
        if q: windows.append(q)
        else:
            limit=number(plan.get('limit'))
            used=number(plan.get('includedSpend',0))
            if limit and limit>0 and used is not None:
                windows.append(Quota('套餐额度 · 月度',(limit-used)/limit*100,reset,
                    f'包含额度 ${limit/100:.2f} · 已用 ${max(0,used)/100:.2f}'))
    if windows:
        return Snapshot('cursor','live','已连接','Cursor 客户端套餐用量接口',time.time() if now is None else now,windows)
    for grant in data.get('activeGrants',[]):
        if not isinstance(grant,dict): continue
        total = number(grant.get('totalCents'))
        remaining = number(grant.get('remainingCents'))
        if total is None or total<=0 or remaining is None: continue
        tags = grant.get('allowedModelTags') or []
        model_ids = grant.get('allowedModelIds') or []
        label = grant.get('displayName') or grant.get('name')
        if not label:
            tag_text = ' '.join(str(t) for t in tags).lower()
            label = 'Cursor 模型额度池' if any(t in tag_text for t in ('composer','cursor','grok')) else '其他模型额度池' if any(t in tag_text for t in ('third','other')) else '套餐额度池'
        if model_ids and label == '套餐额度池': label = ', '.join(model_ids[:2])
        reset = number(grant.get('expiresAtMs'))
        detail = f'剩余 ${max(0,remaining)/100:.2f} / ${total/100:.2f}'
        windows.append(Quota(label,remaining/total*100,reset/1000 if reset else None,detail))
    return Snapshot('cursor','live' if windows else 'waiting', '已连接' if windows else '账号接口未返回可计算的套餐额度',
                    'Cursor 客户端额度接口',time.time() if now is None else now,windows)

def collect_cursor(network):
    db_path = cursor_database()
    if not db_path.exists(): raise FileNotFoundError('Cursor login')
    with sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        row = db.execute('select value from ItemTable where key=?',('cursorAuth/accessToken',)).fetchone()
    if not row or not row[0]: raise PermissionError()
    token = row[0]
    if token.startswith('"'): token=json.loads(token)
    with http_session(network) as session:
        def read(method):
            r = session.post('https://api2.cursor.sh/aiserver.v1.DashboardService/'+method,
            headers={'Authorization':'Bearer '+token,'Connect-Protocol-Version':'1','Content-Type':'application/json'},
            # Cursor's quota service can take over 30 seconds for a valid
            # account response. Keep the connection timeout short, but allow
            # that response to finish before preserving a stale reading.
            json={},timeout=(5,60),allow_redirects=False)
            if r.status_code in (401,403): raise PermissionError()
            r.raise_for_status()
            return parse_cursor(r.json())
        current=read('GetCurrentPeriodUsage')
        if current.quotas: return current
        return read('GetUsageLimitStatusAndActiveGrants')

def parse_claude(data):
    quotas = []
    rate = data.get('rate_limits') or {}
    for key,label in [('five_hour','5 小时'),('seven_day','7 天')]:
        w = rate.get(key)
        if not isinstance(w,dict): continue
        q = Quota.from_used(label,w.get('used_percentage'),w.get('resets_at'))
        if q: quotas.append(q)
    return Snapshot('claude','live' if quotas else 'waiting',
        '已连接' if quotas else '等待 Claude 会话返回套餐额度；旧版客户端可能不提供此字段',
        'Claude Code 官方状态栏',number(data.get('collected_at')),quotas)

def parse_claude_oauth(data):
    from .claude_oauth import quota_timestamp
    quotas = []
    for key,name in [('five_hour','5 小时'),('seven_day','7 天'),
                     ('seven_day_opus','Opus · 7 天'),('seven_day_sonnet','Sonnet · 7 天')]:
        window = data.get(key)
        if not isinstance(window,dict): continue
        quota = Quota.from_used(name,window.get('utilization'),quota_timestamp(window.get('resets_at')))
        if quota: quotas.append(quota)
    return Snapshot('claude','live' if quotas else 'waiting',
        '已连接' if quotas else '已登录 · 账号暂未返回可读取的套餐额度',
        'Claude OAuth 用量接口',time.time(),quotas)

def collect_claude(directory,network=None):
    from .claude_oauth import claude_client
    data = claude_client(directory).usage(network or {'mode':'system'})
    if data is not None: return parse_claude_oauth(data)
    path = Path(directory)/'claude-quota.json'
    if not path.exists():
        return Snapshot('claude',message='尚未连接 · 点击连接管理进行浏览器 OAuth 登录',source='Claude OAuth 待连接')
    return parse_claude(read_json(path,{}))

def collect_manual(provider, settings):
    manual = settings.get('manual',{}).get(provider)
    if not isinstance(manual,dict) or number(manual.get('remaining')) is None:
        return Snapshot(provider,'unsupported','已安装 · 自动余额接口待接入' if provider=='doubao' else '等待连接','尚未取得套餐余额')
    return Snapshot(provider,'manual','手动录入', '手动录入',manual.get('collected_at'),
                    [Quota(manual.get('label') or '套餐权益',manual['remaining'],manual.get('reset_at'))])

def collect(provider,settings,directory):
    try:
        if provider=='codex': return collect_codex(provider_network(settings,provider))
        if provider=='cursor': return collect_cursor(provider_network(settings,provider))
        if provider=='doubao':
            from .doubao import collect_doubao
            network=provider_network(settings,provider) if 'network' in settings else {'mode':'direct','url':''}
            snapshot=collect_doubao(directory,network)
            manual=collect_manual(provider,settings)
            return manual if not snapshot.quotas and manual.quotas else snapshot
        if provider=='claude':
            snapshot=collect_claude(directory,provider_network(settings,provider))
            manual=collect_manual(provider,settings)
            return manual if not snapshot.quotas and manual.quotas else snapshot
        return collect_manual(provider,settings)
    except PermissionError:
        message='登录已过期或账号无额度读取权限，请在原工具重新登录'
    except FileNotFoundError:
        message='未找到本机客户端或登录信息'
    except (requests.Timeout, TimeoutError):
        message='额度响应超时 · 等待自动刷新' if provider=='cursor' else '请求超时，请检查代理设置'
    except requests.exceptions.ProxyError:
        message='代理不可达，请检查地址和端口'
    except requests.exceptions.SSLError:
        message='TLS 连接失败，请检查网络和代理'
    except requests.HTTPError as error:
        response=error.response
        status=response.status_code if response is not None else None
        if provider=='cursor' and status is not None and status>=500:
            message='额度服务异常 · 等待自动刷新'
        elif provider=='cursor' and status==429:
            message='请求限流 · 等待自动刷新'
        else: message='额度接口暂不可用，请稍后刷新或检查代理'
    except requests.RequestException:
        message='额度接口暂不可用，请稍后刷新或检查代理'
    except Exception as error:
        from .claude_oauth import OAuthError
        if isinstance(error,OAuthError): return Snapshot(provider,'error',str(error))
        # Never surface raw exception strings: they can contain headers, URLs or
        # credential-bearing client output.
        message='无法读取额度，客户端格式可能已变化'
    return Snapshot(provider,'error',message)

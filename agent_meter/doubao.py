"""Read quota through the signed-in Doubao renderer; never export credentials."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests
import websocket
from .model import Quota, Snapshot, number
from .storage import validate_proxy
from .doubao_session import read_http_quota, SessionError

PORT = 9225
SOURCE = '豆包桌面版 · 订阅与额度汇总'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

class DoubaoError(Exception):
    """Only fixed, safe messages may cross the renderer boundary."""

# The desktop's own service signs this one read-only quota request. Return only
# whitelisted quota fields, never account details, cookies, or conversations.
QUOTA_EXPRESSION = r'''(async()=>{
  const loader=window['@flow-web/desktop:stable'];
  if(!loader) throw new Error('quota runtime unavailable');
  let timer;
  try {
    const req=await Promise.race([
      new Promise(resolve=>loader.push([['agent_meter_quota_'+crypto.randomUUID()],{},resolve])),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('timeout')),5000);})
    ]);
    const matches=Object.entries(req.m).filter(([,factory])=>{
      const source=factory.toString();
      return source.includes('initApiService') && source.includes('"commerceSale"');
    });
    if(matches.length!==1) throw new Error('quota service ambiguous');
    const api=Object.values(req(Number(matches[0][0])))
      .find(value=>typeof value?.AGWGetSubscriptionQuotaSummary==='function');
    if(!api) throw new Error('quota service unavailable');
    const response=await api.AGWGetSubscriptionQuotaSummary({product_line:'membership'});
    const data=response.data||{};
    return {
      code:response.code,
      subscription:String(data.current_subscription?.display?.short_name||'').slice(0,80),
      groups:(data.window_limit_section?.window_limit_groups||[]).slice(0,16).map(group=>({
        name:String(group.feature_group_name||'').slice(0,80),
        windows:(group.window_limits||[]).slice(0,16).map(window=>({
          window_type:window.window_type,
          used_percent:window.used_percent,
          less_than_one_percent:window.less_than_one_percent,
          end_time:window.end_time,
          exemption:window.exemption?{
            active:window.exemption.active,end_time:window.exemption.end_time
          }:null
        }))
      }))
    };
  } finally {clearTimeout(timer);}
})()'''

def valid_target(target):
    try:
        page = urlparse(target.get('url', ''))
        ws = urlparse(target.get('webSocketDebuggerUrl', ''))
        return (target.get('type') == 'page'
                and page.scheme in ('doubao', 'chrome') and page.hostname == 'doubao-chat'
                and ws.scheme == 'ws' and ws.hostname == '127.0.0.1' and ws.port == PORT
                and not ws.username and not ws.password and not ws.query and not ws.fragment
                and ws.path.startswith('/devtools/page/'))
    except (ValueError, TypeError, AttributeError):
        return False

def quota_target():
    try:
        # This local connection must never leave the machine through a proxy.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f'http://127.0.0.1:{PORT}/json/list',
                                   timeout=(2, 3), allow_redirects=False)
            response.raise_for_status()
            targets = response.json()
    except requests.ConnectionError:
        raise DoubaoError('尚未连接 · 请在连接管理中启动豆包并连接') from None
    except (requests.RequestException, ValueError):
        raise DoubaoError('豆包本机查询接口暂不可用，请稍后刷新') from None
    matches = [t for t in targets if isinstance(t, dict) and valid_target(t)] if isinstance(targets, list) else []
    if len(matches) != 1:
        raise DoubaoError('未找到唯一的豆包聊天页面，请在豆包中打开聊天首页')
    return matches[0]['webSocketDebuggerUrl']

def read_quota():
    address = quota_target()
    connection = None
    try:
        connection = websocket.create_connection(address, timeout=20, suppress_origin=True,
            http_no_proxy=['127.0.0.1', 'localhost'], redirect_limit=0)
        connection.send(json.dumps({'id':1, 'method':'Runtime.evaluate', 'params':{
            'expression':QUOTA_EXPRESSION, 'awaitPromise':True, 'returnByValue':True,
            'timeout':18000}}))
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            connection.settimeout(max(.1, deadline-time.monotonic()))
            response = json.loads(connection.recv())
            if response.get('id') != 1:
                continue
            result = response.get('result') or {}
            if response.get('error') or result.get('exceptionDetails'):
                raise DoubaoError('豆包额度接口未就绪，请确认已登录；客户端更新后接口可能变化')
            value = (result.get('result') or {}).get('value')
            if not isinstance(value, dict):
                raise DoubaoError('豆包未返回有效的额度汇总')
            return value
        raise DoubaoError('读取豆包额度超时，请检查豆包自身的网络设置')
    except DoubaoError:
        raise
    except Exception:
        raise DoubaoError('豆包额度查询失败，请检查登录状态和豆包自身的网络设置') from None
    finally:
        if connection is not None:
            connection.close()

def parse_quota(data, now=None):
    now = time.time() if now is None else now
    if data.get('code') != 0:
        return Snapshot('doubao', 'waiting', '豆包未返回额度，请先在桌面版登录', SOURCE)
    quotas = []
    for group in data.get('groups', []):
        if not isinstance(group, dict): continue
        group_name = str(group.get('name') or '')[:80]
        for window in group.get('windows', []):
            if not isinstance(window, dict): continue
            kind = window.get('window_type')
            name = {1:'当前时段 · 5 小时', 2:'7 天', 3:'套餐周期', 4:'月度'}.get(kind, '额度窗口')
            if group_name: name = group_name+' · '+name
            end = number(window.get('end_time'))
            reset = end/1000 if end and end>0 else None
            exemption = window.get('exemption') or {}
            exempt_end = number(exemption.get('end_time')) if isinstance(exemption, dict) else None
            if isinstance(exemption, dict) and exemption.get('active') is True and exempt_end and exempt_end/1000 > now:
                quotas.append(Quota(name, 100, exempt_end/1000, '额度豁免期间不限量', '不限量'))
                continue
            raw = window.get('used_percent')
            used = None if isinstance(raw, bool) else number(raw)
            if used is None or not 0<=used<=100: continue
            small = window.get('less_than_one_percent') is True and used < 1
            quotas.append(Quota(name, 99 if small else 100-used, reset,
                                '已用 <1%' if small else f'已用 {used:g}%', '>99%' if small else ''))
    plan = str(data.get('subscription') or '')[:80]
    return Snapshot('doubao', 'live' if quotas else 'waiting',
                    ('已连接'+(' · '+plan if plan else '')) if quotas else '已登录 · 暂无可读取的额度窗口',
                    SOURCE, now, quotas)

def collect_doubao(directory=None, network=None):
    error_message = None
    if directory is not None:
        try:
            snapshot = parse_quota(read_http_quota(directory, network or {'mode':'direct'}))
            snapshot.source = '豆包 · 独立订阅额度查询'
            return snapshot
        except SessionError as error:
            error_message = str(error)
    try:
        return parse_quota(read_quota())
    except DoubaoError as error:
        return Snapshot('doubao', 'waiting', error_message or str(error),
                        '豆包 · 独立订阅额度查询' if directory is not None else SOURCE)

def installed_app():
    if sys.platform == 'darwin':
        from .platform_support import mac_app_executable
        path = mac_app_executable('Doubao') or mac_app_executable('豆包')
        if path: return path
        raise DoubaoError('未找到豆包桌面版，请先安装并登录')
    base = Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'Doubao/Application'
    for path in (base/'app/Doubao.exe', base/'Doubao.exe'):
        if path.is_file(): return path
    raise DoubaoError('未找到豆包桌面版，请先安装并登录')

def app_running():
    if sys.platform == 'darwin':
        path = str(installed_app())
        result = subprocess.run(['/bin/ps', '-axo', 'command='],
            capture_output=True, text=True, timeout=4)
        if result.returncode != 0:
            raise DoubaoError('无法检查豆包是否已退出，请稍后重试')
        return any(line == path or line.startswith(path + ' ') for line in result.stdout.splitlines())
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Doubao.exe', '/FO', 'CSV', '/NH'],
        capture_output=True, text=True, timeout=4, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise DoubaoError('无法检查豆包是否已退出，请稍后重试')
    return any(row and row[0].lower() == 'doubao.exe' for row in csv.reader(io.StringIO(result.stdout)))

def start_connection(network):
    """Launch only a closed app. Never terminate a user's running work."""
    try:
        quota_target()
        return '已找到本机豆包，正在读取额度…'
    except DoubaoError:
        pass
    if app_running():
        raise DoubaoError('请保存任务并完全退出豆包，再点击“启动豆包并连接”；不会自动关闭运行中的豆包')
    args = [str(installed_app()), '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={PORT}']
    if network.get('mode') == 'custom':
        proxy = validate_proxy(network.get('url', ''))
        if proxy.startswith('socks5h://'): proxy = 'socks5://'+proxy[len('socks5h://'):]
        args.append('--proxy-server='+proxy)
    elif network.get('mode') == 'direct':
        args.append('--no-proxy-server')
    subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
    return '豆包已启动 · 请确认桌面版已登录，稍后自动读取额度'

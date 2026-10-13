"""Official Codex client sign-in and per-provider connection descriptions."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from .providers import codex_command, proxy_environment, CREATE_NO_WINDOW
from . import __version__

USAGE_URLS = {'cursor':'https://cursor.com/dashboard?tab=usage',
              'codex':'https://chatgpt.com/codex/settings/usage',
              'claude':'https://claude.ai/settings/usage', 'doubao':'https://www.doubao.com/'}

def connection_description(provider):
    return {
        'cursor':'优先读取本机 Cursor 登录；重新登录请在 Cursor 客户端完成。',
        'codex':'优先复用本机 Codex 登录；也可通过官方组件进行浏览器 OAuth。',
        'claude':'优先复用本机有效登录；没有客户端也可通过浏览器 OAuth 登录。',
        'doubao':'优先复用本机豆包登录，独立读取当前时段及周期额度。首次复用请登录后退出一次；会话加密保存在本机，之后无需保持豆包运行。',
    }[provider]

def open_local_cursor():
    if sys.platform == 'darwin':
        from .platform_support import mac_app
        app = mac_app('Cursor')
        if not app: return False
        subprocess.Popen(['/usr/bin/open', str(app)])
        return True
    candidates = [Path(os.environ.get('LOCALAPPDATA',''))/'Programs/cursor/Cursor.exe',
                  Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Cursor/Cursor.exe']
    for path in candidates:
        if path.is_file():
            subprocess.Popen([str(path)],creationflags=CREATE_NO_WINDOW)
            return True
    return False

def official_auth_url(url):
    try:
        parsed=urlparse(url)
        return parsed.scheme=='https' and parsed.hostname in ('auth.openai.com','chatgpt.com') and parsed.port in (None,443) and not parsed.username and not parsed.password
    except ValueError: return False

def codex_login(network, cancelled, on_url, on_status, command=None):
    """Keep official app-server alive through its own OAuth callback."""
    process=subprocess.Popen((command or codex_command())+['app-server'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
        env=proxy_environment(network),creationflags=CREATE_NO_WINDOW)
    inbox=queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                try: inbox.put(json.loads(line))
                except ValueError: pass
        finally: inbox.put(None)
    thread=threading.Thread(target=reader,daemon=True); thread.start()
    def send(method,ident=None,params=None):
        msg={'method':method}
        if ident is not None: msg['id']=ident
        if params is not None: msg['params']=params
        process.stdin.write(json.dumps(msg)+'\n'); process.stdin.flush()
    login_id=None; started=False; completed=False; deadline=time.monotonic()+180
    try:
        send('initialize',1,{'clientInfo':{'name':'agent_meter','version':__version__}})
        while time.monotonic()<deadline and not cancelled.is_set():
            try: message=inbox.get(timeout=.25)
            except queue.Empty: continue
            if message is None: return False,'官方登录组件已退出，请重试。'
            if message.get('error'): return False,'无法启动登录，请检查代理或组件版本。'
            if message.get('id')==1:
                send('initialized'); send('account/login/start',2,{'type':'chatgpt'})
            elif message.get('id')==2:
                result=message.get('result') or {}; login_id=result.get('loginId')
                url=result.get('authUrl','')
                if not login_id or not official_auth_url(url): return False,'官方组件未返回有效的登录地址。'
                started=True; on_status('请在浏览器完成登录；本窗口会自动检查结果。'); on_url(url)
            elif message.get('method')=='account/login/completed':
                result=message.get('params') or {}
                if not started or result.get('loginId')!=login_id: continue
                completed=True
                success=result.get('success') is True
                return success,'登录完成，正在刷新额度。' if success else '登录未完成，请重试。'
        return False,'已取消登录。' if cancelled.is_set() else '登录等待超时，请重试。'
    finally:
        if login_id and not completed and process.poll() is None:
            try: send('account/login/cancel',3,{'loginId':login_id})
            except (OSError,ValueError): pass
        if process.poll() is None: process.kill()
        process.wait(timeout=5); thread.join(timeout=2)
        for stream in (process.stdin,process.stdout):
            if stream: stream.close()

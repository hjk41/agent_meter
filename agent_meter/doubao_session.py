"""Read-only consumer membership quotas with platform-encrypted sessions.

Protocol research: OxygenAILab/AskDoubao. No generation endpoints are used.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
import requests

from .claude_oauth import dpapi
from .storage import atomic_json, read_json

QUOTA_URL = 'https://www.doubao.com/alice/commerce/sale/subscription/quota/summary/'
COOKIE_NAMES = frozenset(('sessionid', 'sessionid_ss', 'passport_csrf_token',
    'passport_csrf_token_default', 'ttwid', 'msToken', 's_v_web_id', 'device_id', 'odin_tt'))
COOKIE_HOSTS = ('.doubao.com', 'doubao.com', 'www.doubao.com')

class SessionError(Exception):
    """Messages contain no upstream payloads or credentials."""

def clean_cookies(cookies):
    if not isinstance(cookies, dict): return {}
    return {name:value for name,value in cookies.items() if name in COOKIE_NAMES
            and isinstance(value,str) and 0<len(value)<=16384
            and not any(c in value for c in '\r\n\x00')}

def decrypt_cookie(blob, key, host, mac=False):
    if blob[:3] not in (b'v10', b'v11'):
        raise SessionError('本机登录加密格式暂不支持，请使用桌面版连接')
    if mac:
        if blob[:3] != b'v10':
            raise SessionError('本机登录加密格式暂不支持，请使用桌面版连接')
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b' ' * 16)).decryptor()
        padded = decryptor.update(blob[3:]) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
    else:
        raw = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
    digest = hashlib.sha256(host.encode('utf-8')).digest()
    if raw[:32] == digest: raw = raw[32:]
    return raw.decode('utf-8')

def local_cookies(profile=None):
    """Open only Doubao's cookie DB read-only; never copy a browser profile."""
    mac = sys.platform == 'darwin'
    if profile is None:
        profile = (Path.home()/'Library/Application Support/Doubao' if mac else
            Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'Doubao/User Data')
    profile = Path(profile)
    try:
        if mac:
            from .secure_storage import keychain_backend
            password = keychain_backend().get_password('Doubao Safe Storage', 'Doubao')
            if not password: raise ValueError()
            key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), b'saltysalt', 1003, 16)
        else:
            state = json.loads((profile/'Local State').read_text(encoding='utf-8'))
            encrypted = base64.b64decode(state['os_crypt']['encrypted_key'], validate=True)
            if not encrypted.startswith(b'DPAPI'): raise ValueError()
            key = dpapi(encrypted[5:], True)
        database = profile/'Default/Network/Cookies'
        if not database.is_file(): database = profile/'Default/Cookies'
        connection = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)
        try:
            hosts = ','.join('?' for _ in COOKIE_HOSTS)
            names = ','.join('?' for _ in COOKIE_NAMES)
            rows = connection.execute(f'SELECT host_key,name,value,encrypted_value FROM cookies '
                f'WHERE host_key IN ({hosts}) AND name IN ({names})', (*COOKIE_HOSTS,*COOKIE_NAMES))
            cookies = {}
            for host,name,value,blob in rows:
                try: value = value or decrypt_cookie(blob,key,host,mac=mac)
                except Exception: continue
                if value: cookies[name] = value
        finally:
            connection.close()
        cookies = clean_cookies(cookies)
        if not cookies.get('sessionid'): raise ValueError()
        return cookies
    except Exception:
        raise SessionError('暂时无法复用本机登录，请登录豆包并退出一次，再刷新连接') from None

class SessionStore:
    def __init__(self, directory):
        suffix = 'keychain' if sys.platform == 'darwin' else 'dpapi'
        self.path = Path(directory)/f'doubao-session.{suffix}.json'

    def load(self):
        try:
            envelope = read_json(self.path,{})
            if envelope.get('version') != 1: return None
            cookies = clean_cookies(json.loads(dpapi(base64.b64decode(envelope['blob'],validate=True),True)))
            return cookies if cookies.get('sessionid') else None
        except Exception: return None

    def save(self, cookies):
        try:
            encrypted = dpapi(json.dumps(clean_cookies(cookies)).encode('utf-8'))
            atomic_json(self.path,{'version':1,'blob':base64.b64encode(encrypted).decode('ascii')})
        except Exception:
            raise SessionError('额度已读取，但登录会话无法加密保存，请检查本机存储权限') from None

    def delete(self):
        self.path.unlink(missing_ok=True)

def quota_fields(response):
    """Discard account identifiers and billing details before parsing or caching."""
    data = response.get('data') or {}
    groups = data.get('window_limit_section',{}).get('window_limit_groups') or []
    return {'code':response.get('code'),
        'subscription':str((data.get('current_subscription') or {}).get('display',{}).get('short_name') or '')[:80],
        'groups':[{'name':str(group.get('feature_group_name') or '')[:80],
            'windows':[{**{name:window.get(name) for name in
                ('window_type','used_percent','less_than_one_percent','end_time')},
                'exemption':{name:window['exemption'].get(name) for name in ('active','end_time')}
                if isinstance(window.get('exemption'),dict) else None}
                for window in (group.get('window_limits') or [])[:16] if isinstance(window,dict)]}
            for group in groups[:16] if isinstance(group,dict)]}

def read_http_quota(directory, network, session_factory=None):
    store = SessionStore(directory)
    try: cookies = local_cookies()
    except SessionError:
        cookies = store.load()
    if not cookies: raise SessionError('尚未连接 · 请登录豆包并退出一次，然后刷新连接')
    # Share the same proxy selection as the other collectors, including direct mode.
    if session_factory is None:
        from .providers import http_session
        session_factory = http_session
    headers = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        'Content-Type':'application/json','Origin':'https://www.doubao.com',
        'Referer':'https://www.doubao.com/chat/','agw-js-conv':'str, str'}
    csrf = cookies.get('passport_csrf_token') or cookies.get('passport_csrf_token_default')
    if csrf: headers['x-tt-passport-csrf-token'] = csrf
    params = {'aid':'497858','real_aid':'497858','device_platform':'web','language':'zh',
        'pkg_type':'release_version','version_code':'20800','chromium_version':'148.0.7816.0',
        'samantha_web':'1','use-olympus-account':'1'}
    if cookies.get('s_v_web_id'): params['fp'] = cookies['s_v_web_id']
    if cookies.get('device_id'):
        params['device_id'] = params['tea_uuid'] = cookies['device_id']
    try:
        with session_factory(network) as session:
            for name,value in cookies.items():
                session.cookies.set(name,value,domain='.doubao.com',path='/')
            response = session.post(QUOTA_URL,json={'product_line':'membership'},params=params,
                headers=headers,timeout=(5,15),allow_redirects=False)
            if response.status_code in (401,403):
                raise SessionError('豆包登录已失效或需要验证，请在官方客户端重新登录后刷新')
            if response.status_code != 200:
                raise SessionError('豆包额度接口暂不可用，请稍后刷新')
            response.encoding = 'utf-8'
            body = response.json()
            if not isinstance(body,dict) or body.get('code') != 0:
                raise SessionError('豆包会话需要重新登录或验证，请在官方客户端处理后刷新')
            result = quota_fields(body)
        # Persist only after acceptance, using DPAPI or a Keychain-held AES key.
        store.save(cookies)
        return result
    except SessionError: raise
    except requests.Timeout:
        raise SessionError('豆包额度请求超时，请检查代理设置') from None
    except requests.RequestException:
        raise SessionError('豆包额度查询失败，请检查网络和代理设置') from None
    except Exception:
        raise SessionError('豆包返回的额度数据暂无法读取，请稍后刷新') from None

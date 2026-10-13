"""Browser PKCE login and read-only Claude subscription quota requests.

Protocol reference: character-s/claude-usage-bar-windows. This is an
independent implementation; the endpoints are not a supported public API.
"""
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from .storage import atomic_json, read_json

AUTHORIZE_URL = 'https://claude.ai/oauth/authorize'
TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
REDIRECT_URI = 'https://platform.claude.com/oauth/code/callback'
CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
SCOPES = ['user:profile', 'user:inference']

class OAuthError(Exception):
    """Only user-safe messages may be stored in this exception."""

class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise OAuthError('令牌加密存储需要 Windows。')
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), 'Agent Meter Claude OAuth', None, None, None, 1, ctypes.byref(output))
    function.restype = wintypes.BOOL
    if not function(*args):
        raise OAuthError('无法解密登录信息，请重新登录。' if decrypt else '无法加密保存登录信息。')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))

class CredentialStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'claude-oauth.dpapi.json'

    def load(self):
        if not self.path.exists(): return None
        try:
            envelope = read_json(self.path, {})
            if envelope.get('version') != 1: raise ValueError()
            data = json.loads(dpapi(base64.b64decode(envelope['blob'], validate=True), True))
            if not isinstance(data, dict) or not data.get('access_token'): raise ValueError()
            return data
        except OAuthError: raise
        except Exception: raise OAuthError('登录信息无法读取，请重新登录。') from None

    def save(self, credentials):
        encrypted = dpapi(json.dumps(credentials).encode('utf-8'))
        atomic_json(self.path, {'version': 1, 'blob': base64.b64encode(encrypted).decode('ascii')})

    def delete(self):
        self.path.unlink(missing_ok=True)

@dataclass
class LoginFlow:
    verifier: str = field(repr=False)
    state: str = field(repr=False)
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        return AUTHORIZE_URL + '?' + urlencode({'code':'true', 'client_id':CLIENT_ID,
            'response_type':'code', 'redirect_uri':REDIRECT_URI, 'scope':' '.join(SCOPES),
            'code_challenge':challenge, 'code_challenge_method':'S256', 'state':self.state})

def begin_login():
    return LoginFlow(secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.monotonic()+600)

def authorization_code(raw, flow):
    if flow.cancelled.is_set(): raise OAuthError('登录已取消，请重新开始。')
    if time.monotonic() > flow.deadline: raise OAuthError('授权已超时，请重新开始登录。')
    raw = raw.strip()
    returned_state = None
    if '://' in raw:
        try:
            p = urlparse(raw)
            if p.scheme != 'https' or p.hostname != 'platform.claude.com' or p.port not in (None,443) or p.username or p.password or p.path != '/oauth/code/callback':
                raise ValueError()
            params = parse_qs(p.query)
            if 'error' in params: raise ValueError()
            code = params.get('code',[''])[0]
            returned_state = params.get('state',[''])[0]
            if '#' in code: code, returned_state = code.split('#',1)
        except ValueError: raise OAuthError('请粘贴 Claude 授权码或正确的回调地址。') from None
    else:
        code, separator, returned_state = raw.partition('#')
        if not separator: returned_state = None  # Official page may return only a code.
    if returned_state is not None and not secrets.compare_digest(returned_state,flow.state):
        raise OAuthError('授权状态不匹配，请重新开始登录。')
    if not code or len(code)>4096 or any(c.isspace() or ord(c)<32 for c in code):
        raise OAuthError('请粘贴有效的授权码。')
    return code

def token_response(data, previous=None):
    previous = previous or {}
    if not isinstance(data,dict) or not isinstance(data.get('access_token'),str) or not data['access_token']:
        raise OAuthError('登录服务返回了无效令牌，请重新登录。')
    try:
        lifetime = float(data['expires_in'])
        if not 0 < lifetime < 31536000: raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise OAuthError('登录服务未返回有效的令牌期限。') from None
    scopes = data.get('scope')
    scopes = scopes.split() if isinstance(scopes,str) else previous.get('scopes',SCOPES)
    if 'user:profile' not in scopes:
        raise OAuthError('本次授权没有额度读取权限，请重新登录。')
    refresh = data.get('refresh_token') or previous.get('refresh_token')
    if refresh is not None and not isinstance(refresh,str): raise OAuthError('登录服务返回了无效令牌。')
    return {'access_token':data['access_token'], 'refresh_token':refresh,
            'expires_at':time.time()+lifetime, 'scopes':scopes}

def local_credentials():
    """Read an existing CLI login without copying or rotating its refresh token."""
    data = read_json(Path.home()/'.claude/.credentials.json', {}).get('claudeAiOauth')
    if not isinstance(data,dict) or not data.get('accessToken') or 'user:profile' not in (data.get('scopes') or []): return None
    try: expires = float(data.get('expiresAt',0))/1000
    except (TypeError,ValueError): expires = 0
    if expires <= time.time(): return None
    return {'access_token':data['accessToken'], 'expires_at':expires, 'scopes':data['scopes']}

def session_for(network):
    from .providers import http_session
    return http_session(network)

class ClaudeClient:
    def __init__(self, directory):
        self.store = CredentialStore(directory)
        self._operation = threading.Lock()
        self._state = threading.RLock()
        self.generation = 0
        self.blocked_until = 0
        self.backoff = 60

    def forget(self):
        with self._state:
            self.generation += 1
            self.store.delete()
            self.blocked_until = 0
            self.backoff = 60

    def _save(self, credentials, generation, cancelled=None):
        with self._state:
            if generation != self.generation or (cancelled and cancelled.is_set()):
                raise OAuthError('登录已取消或账号已更改，请重新开始。')
            self.store.save(credentials)

    def exchange(self, raw, flow, network):
        code = authorization_code(raw,flow)
        with self._state: generation = self.generation
        with self._operation, session_for(network) as session:
            authorization_code(raw,flow)
            response = session.post(TOKEN_URL, json={'grant_type':'authorization_code',
                'code':code, 'state':flow.state, 'client_id':CLIENT_ID,
                'redirect_uri':REDIRECT_URI, 'code_verifier':flow.verifier},
                timeout=(5,20), allow_redirects=False)
            if response.status_code != 200:
                raise OAuthError('授权码交换失败，请重新登录并检查代理。' if response.status_code < 500 else '登录服务暂不可用，请稍后重试。')
            try: credentials = token_response(response.json())
            except ValueError: raise OAuthError('登录服务返回格式异常，请重试。') from None
            with self._state:
                if time.monotonic() > flow.deadline: raise OAuthError('授权已超时，请重新开始登录。')
                self._save(credentials,generation,flow.cancelled)
                flow.cancelled.set()  # Authorization code is one-use.
                self.generation += 1
                self.blocked_until = 0
            return True

    def _refresh(self, session, credentials, generation):
        if not credentials.get('refresh_token'):
            raise OAuthError('本机 Claude 登录已过期，请使用浏览器 OAuth 重新登录。')
        response = session.post(TOKEN_URL, json={'grant_type':'refresh_token',
            'refresh_token':credentials['refresh_token'], 'client_id':CLIENT_ID},
            timeout=(5,20), allow_redirects=False)
        if response.status_code in (400,401,403): raise OAuthError('Claude 授权已失效，请重新登录。')
        if response.status_code != 200: raise OAuthError('令牌刷新暂时失败，请稍后重试或检查代理。')
        try: updated = token_response(response.json(),credentials)
        except ValueError: raise OAuthError('登录服务返回格式异常。') from None
        self._save(updated,generation)
        return updated

    def usage(self, network):
        with self._operation:
            with self._state:
                if time.monotonic() < self.blocked_until:
                    raise OAuthError('Claude 请求限流，暂停重试中；上次读数保留。')
                generation = self.generation
                own = self.store.load()
            credentials = own or local_credentials()
            if not credentials: return None
            with session_for(network) as session:
                refreshed = False
                if credentials['expires_at'] <= time.time()+60 and own:
                    credentials = self._refresh(session,credentials,generation)
                    refreshed = True
                def read():
                    return session.get(USAGE_URL,headers={'Authorization':'Bearer '+credentials['access_token'],
                        'anthropic-beta':'oauth-2025-04-20'}, timeout=(5,20),allow_redirects=False)
                response = read()
                if response.status_code == 401 and own and not refreshed:
                    credentials = self._refresh(session,credentials,generation)
                    response = read()
                if response.status_code in (401,403):
                    raise OAuthError('Claude 登录已失效或无额度读取权限，请重新登录。')
                if response.status_code == 429:
                    retry = response.headers.get('Retry-After','')
                    try: seconds = float(retry)
                    except ValueError:
                        try: seconds = parsedate_to_datetime(retry).timestamp()-time.time()
                        except (ValueError,TypeError,OverflowError): seconds = 0
                    with self._state:
                        self.backoff = min(self.backoff*2,3600)
                        self.blocked_until = time.monotonic()+max(self.backoff,seconds)
                    raise OAuthError('Claude 请求限流，已暂停重试；上次读数保留。')
                if response.status_code != 200: raise OAuthError('Claude 额度接口暂不可用，请稍后刷新。')
                try: data = response.json()
                except ValueError: raise OAuthError('Claude 额度返回格式异常。') from None
                if not isinstance(data,dict): raise OAuthError('Claude 额度返回格式异常。')
                with self._state:
                    if generation != self.generation: raise OAuthError('账号已更改，正在重新读取额度。')
                    self.backoff = 60
                return data

_clients = {}
_clients_lock = threading.Lock()

def claude_client(directory):
    key = str(Path(directory).resolve())
    with _clients_lock:
        if key not in _clients: _clients[key] = ClaudeClient(directory)
        return _clients[key]

def quota_timestamp(value):
    if not isinstance(value,str): return value
    try:
        result = datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None: result = result.replace(tzinfo=timezone.utc)
        return result.timestamp()
    except ValueError: return None

def safe_login_error(error):
    if isinstance(error,OAuthError): return str(error)
    if isinstance(error,requests.exceptions.ProxyError): return '代理不可达，请检查代理地址。'
    if isinstance(error,requests.exceptions.SSLError): return 'TLS 连接失败，请检查代理。'
    if isinstance(error,requests.Timeout): return '登录请求超时，请检查代理并重试。'
    if isinstance(error,requests.RequestException): return '登录服务连接失败，请检查代理并重试。'
    return '无法完成登录，请重新开始。'

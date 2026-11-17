"""Read Factory Droid's account limits using its existing local login."""
import base64
import json
import os
from pathlib import Path
import sys
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .claude_oauth import quota_timestamp
from .model import Quota, Snapshot, number

LIMITS_URL = 'https://api.factory.ai/api/billing/limits'
SOURCE = 'Factory Droid 额度接口'
AUTH_SERVICE = 'Factory CLI'
AUTH_ACCOUNT = 'auth-encryption-key'
WINDOWS = (('fiveHour', '5 小时'), ('weekly', '7 天'), ('monthly', '30 天'))


def local_encryption_key():
    if sys.platform == 'win32':
        from win32ctypes.pywin32 import win32cred
        credential = win32cred.CredRead(f'{AUTH_SERVICE}/{AUTH_ACCOUNT}', win32cred.CRED_TYPE_GENERIC)
        return credential['CredentialBlob']
    if sys.platform == 'darwin':
        from .secure_storage import keychain_backend
        return keychain_backend().get_password(AUTH_SERVICE, AUTH_ACCOUNT)
    return None


def local_access_token():
    path = Path.home()/'.factory/auth.v2.keyring'
    if not path.is_file():
        return None
    encoded_key = local_encryption_key()
    if not encoded_key:
        return None
    # The keyring holds the AES-256 key. The file stores IV, tag and ciphertext
    # as separate base64 fields. AESGCM expects the tag after the ciphertext.
    key = base64.b64decode(encoded_key, validate=True)
    if len(key) != 32:
        raise ValueError('Invalid Factory encryption key')
    iv, tag, ciphertext = (base64.b64decode(part, validate=True)
                           for part in path.read_text(encoding='utf-8').strip().split(':'))
    login = json.loads(AESGCM(key).decrypt(iv, ciphertext + tag, None))
    token = login.get('access_token') if isinstance(login, dict) else None
    return token if isinstance(token, str) and token else None


def parse_droid(data, now=None):
    limits = data.get('limits') if isinstance(data, dict) else None
    quotas = []
    if isinstance(limits, dict) and not limits.get('notAvailable'):
        for pool_key, pool_name in (('standard', '标准'), ('core', 'Core')):
            pool = limits.get(pool_key)
            if not isinstance(pool, dict):
                continue
            for window_key, window_name in WINDOWS:
                window = pool.get(window_key)
                if not isinstance(window, dict):
                    continue
                used = window.get('usedPercent')
                used = None if isinstance(used, bool) else number(used)
                if used is None or used < 0:
                    continue
                end = window.get('windowEnd')
                reset = quota_timestamp(end) if isinstance(end, str) else None
                quotas.append(Quota.from_used(f'{pool_name} · {window_name}', used, reset))
    return Snapshot('droid', 'live' if quotas else 'waiting',
                    '已连接' if quotas else 'Factory 账号未返回滚动额度',
                    SOURCE, time.time() if now is None else now, quotas)


def collect_droid(network):
    from .providers import http_session

    api_key = os.environ.get('FACTORY_API_KEY', '').strip()
    try:
        token = api_key or local_access_token()
    except (OSError, ValueError, InvalidTag):
        return Snapshot('droid', 'error', '无法读取本机 Droid 登录 · 请在 Factory 客户端重新登录', SOURCE)
    if not token:
        return Snapshot('droid', 'waiting', '尚未找到本机 Droid 登录 · 请先在 Factory 客户端登录', SOURCE)

    with http_session(network) as session:
        response = session.get(LIMITS_URL,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
            timeout=(10, 20), allow_redirects=False)
        if response.status_code in (401, 403):
            message = ('Factory API Key 无效或没有额度读取权限' if api_key else
                       'Droid 登录已过期或无额度读取权限 · 请打开 Factory 客户端刷新登录')
            return Snapshot('droid', 'error', message, SOURCE)
        if response.status_code == 429:
            return Snapshot('droid', 'error', '请求限流 · 等待自动刷新', SOURCE)
        response.raise_for_status()
        return parse_droid(response.json())

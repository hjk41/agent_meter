"""Authenticated encryption with a key held only in the macOS login Keychain."""
import base64
import threading
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_lock = threading.Lock()
SERVICE = 'AgentMeter'
ACCOUNT = 'credential-encryption-key-v1'


def keychain_backend():
    # Select the native backend explicitly; never accept a plaintext fallback.
    from keyring.backends.macOS import Keyring
    return Keyring()


def mac_protect(data, decrypt=False):
    from .claude_oauth import OAuthError
    try:
        with _lock:
            backend = keychain_backend()
            encoded = backend.get_password(SERVICE, ACCOUNT)
            if encoded is None:
                if decrypt:
                    raise ValueError('Missing encryption key')
                encoded = base64.b64encode(AESGCM.generate_key(bit_length=256)).decode('ascii')
                backend.set_password(SERVICE, ACCOUNT, encoded)
            key = base64.b64decode(encoded, validate=True)
            if len(key) != 32:
                raise ValueError('Invalid encryption key')
        cipher = AESGCM(key)
        if decrypt:
            if not data.startswith(b'AMK1') or len(data) < 32:
                raise ValueError('Invalid encrypted envelope')
            return cipher.decrypt(data[4:16], data[16:], b'AgentMeter-v1')
        import secrets
        nonce = secrets.token_bytes(12)
        return b'AMK1' + nonce + cipher.encrypt(nonce, data, b'AgentMeter-v1')
    except Exception:
        raise OAuthError('无法读取或保存加密登录信息，请解锁 macOS 登录钥匙串后重试。') from None

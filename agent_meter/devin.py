"""Read the account quotas used by the local Devin CLI; never start a session."""
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .model import Quota, Snapshot, number

STATUS_PATH = '/exa.seat_management_pb.SeatManagementService/GetUserStatus'
# GetUserStatus validates the client metadata, including both version fields.
# This shape was verified against the installed official CLI and the live service.
CLI_PROTOCOL_VERSION = '3000.11.92'


def credentials_path():
    explicit = os.environ.get('AGENT_METER_DEVIN_CREDENTIALS')
    if explicit: return Path(explicit).expanduser()
    if sys.platform == 'win32':
        root = Path(os.environ.get('APPDATA', Path.home()/'AppData/Roaming'))
    else:
        root = Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')
    return root/'devin/credentials.toml'


def field(data, snake):
    if not isinstance(data, dict): return None
    if snake in data: return data[snake]
    head, *tail = snake.split('_')
    return data.get(head + ''.join(part.title() for part in tail))


def parse_devin(data, now=None):
    user = field(data, 'user_status')
    plan = field(user, 'plan_status')
    info = field(data, 'plan_info')
    quotas = []
    for period, label in (('daily', '每日'), ('weekly', '每周')):
        if field(info, f'hide_{period}_quota') is True: continue
        raw = field(plan, f'{period}_quota_remaining_percent')
        remaining = None if isinstance(raw, bool) else number(raw)
        # Negative sentinel values and absent windows cannot be turned into 100%.
        if remaining is None or not 0 <= remaining <= 100: continue
        reset = field(plan, f'{period}_quota_reset_at_unix')
        reset = None if isinstance(reset, bool) else number(reset)
        quotas.append(Quota(label, remaining, reset if reset and reset > 0 else None))
    return Snapshot('devin', 'live' if quotas else 'waiting',
                    '已连接' if quotas else '账号未返回可计算的每日或每周额度',
                    'Devin CLI 账号额度接口', time.time() if now is None else now, quotas)


def collect_devin(network):
    # Import lazily to share the application's proxy policy without a cycle.
    from .providers import http_session
    credentials = tomllib.loads(credentials_path().read_text(encoding='utf-8-sig'))
    token = credentials.get('windsurf_api_key')
    if not isinstance(token, str) or not token.strip(): raise PermissionError('Devin login')
    server = credentials.get('api_server_url')
    if not isinstance(server, str): raise ValueError('Missing Devin API server')
    parsed = urlsplit(server)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('Invalid Devin API server')
    with http_session(network) as session:
        response = session.post(server.rstrip('/') + STATUS_PATH,
            headers={'Content-Type':'application/json', 'Connect-Protocol-Version':'1'},
            json={'metadata':{'apiKey':token, 'ideName':'devin-cli', 'ideType':'chisel',
                'ideVersion':CLI_PROTOCOL_VERSION, 'extensionName':'chisel',
                'extensionVersion':CLI_PROTOCOL_VERSION, 'locale':'en',
                'os':'windows' if sys.platform=='win32' else sys.platform}},
            # Proxy TLS setup can exceed five seconds even for a valid login.
            timeout=(15, 30), allow_redirects=False)
        if response.status_code in (401, 403): raise PermissionError('Devin login')
        response.raise_for_status()
        return parse_devin(response.json())

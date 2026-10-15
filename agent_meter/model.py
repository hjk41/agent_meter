"""Provider-neutral quota snapshots. Token costs are never subscription balances."""
from dataclasses import asdict, dataclass, field
import math
import time

DEFAULT_REFRESH_SECONDS = 300

PROVIDERS = {'cursor': ('Cursor', 'Pro', 'C'), 'codex': ('Codex', 'Ultra', 'O'),
             'claude': ('Claude Code', 'Max', '✳'), 'doubao': ('豆包', '桌面版', '豆'),
             'devin': ('Devin', 'CLI', 'D')}

def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None

@dataclass
class Quota:
    name: str
    remaining: float
    reset_at: float | None = None
    detail: str = ''
    remaining_display: str = ''

    def __post_init__(self):
        n = number(self.remaining)
        if n is None:
            raise ValueError('Invalid quota')
        self.remaining = min(100, max(0, n))
        self.reset_at = number(self.reset_at)
        if self.remaining_display not in ('', '>99%', '不限量'):
            self.remaining_display = ''

    @classmethod
    def from_used(cls, name, used, reset=None, detail=''):
        n = number(used)
        if n is None:
            return None
        return cls(name, 100-n, reset, detail)

@dataclass
class Snapshot:
    provider: str
    status: str = 'waiting'
    message: str = '等待采集'
    source: str = ''
    collected_at: float | None = None
    quotas: list[Quota] = field(default_factory=list)

    @property
    def primary(self):
        return self.quotas[0] if self.quotas else None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, 'quotas': [Quota(**q) for q in data.get('quotas', [])]})

def merge_snapshot(old, fresh):
    # Preserve the last successful source timestamp on an error; never pretend a
    # new polling attempt refreshed an old measurement.
    if not fresh.quotas and old and old.quotas and fresh.status in ('error','waiting'):
        return Snapshot(fresh.provider, 'stale', fresh.message, old.source,
                        old.collected_at, old.quotas)
    return fresh

def effective_status(snapshot, now=None, refresh_seconds=DEFAULT_REFRESH_SECONDS):
    now = time.time() if now is None else now
    if snapshot.status in ('live', 'manual') and snapshot.quotas:
        if any(q.reset_at and now >= q.reset_at for q in snapshot.quotas):
            return 'stale'
        max_age = max(180, refresh_seconds+60)
        if snapshot.status == 'live' and snapshot.collected_at and now-snapshot.collected_at > max_age:
            return 'stale'
    return snapshot.status

def percent(q):
    return '—' if q is None else q.remaining_display or f'{q.remaining:.0f}%'

def reset_text(reset, now=None):
    if not reset:
        return '重置时间未知'
    left = reset-(time.time() if now is None else now)
    if left <= 0:
        return '窗口已结束 · 等待新读数'
    mins = max(1, int(left/60))
    if mins >= 1440:
        return f'{mins//1440} 天 {mins%1440//60} 小时后重置'
    if mins >= 60:
        return f'{mins//60} 小时 {mins%60} 分后重置'
    return f'{mins} 分后重置'

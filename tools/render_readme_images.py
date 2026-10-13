"""Render README screenshots from native widgets with isolated example data."""
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel
from agent_meter.model import Quota, Snapshot
from agent_meter.storage import Store
from agent_meter.ui import MeterWindow


def main():
    app = QApplication([])
    if sys.platform == 'win32':
        QFont.insertSubstitution('Segoe UI', 'Microsoft YaHei UI')
    output = ROOT / 'docs/images'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='agent-meter-readme-') as directory:
        store = Store(directory)
        store.settings.update(enabled_agents=['codex', 'cursor'],
                              order=['codex', 'cursor', 'claude', 'doubao'],
                              mini_ids=['codex', 'cursor'], mini=False,
                              theme='light', opacity=100, notifications=False)
        window = MeterWindow(store, auto_collect=False)
        now = time.time()
        window.snapshots.update({
            'codex': Snapshot('codex', 'live', '已连接', '示例读数', now,
                              [Quota('7 天', 88, now + 3*86400 + 6*3600 + 30)]),
            'cursor': Snapshot('cursor', 'live', '已连接', '示例读数', now,
                               [Quota('其他模型 · 月度', 81, now + 12*86400 + 30),
                                Quota('Cursor / Auto 模型 · 月度', 46, now + 12*86400 + 30)]),
        })
        window.render()
        window.show()
        app.processEvents()
        assert not any('重置时间未知' in label.text() for label in window.findChildren(QLabel))
        for mini, name in ((False, 'overview.png'), (True, 'minibar.png')):
            store.settings['mini'] = mini
            window.render()
            app.processEvents()
            assert window.grab().save(str(output / name))
            print(f'{name}: {window.width()} x {window.height()} logical pixels')
        window.quitting = True
        window.tray.hide()
        window.close()


if __name__ == '__main__':
    main()

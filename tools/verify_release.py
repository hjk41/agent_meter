"""Offline GUI startup check for the built app, without account credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from agent_meter.storage import Store
    executable = (ROOT/'dist/AgentMeter.exe' if sys.platform == 'win32' else
                  ROOT/'dist/Agent Meter.app/Contents/MacOS/AgentMeter')
    if not executable.is_file(): raise RuntimeError('Missing packaged executable')
    with tempfile.TemporaryDirectory(prefix='agent-meter-release-check-') as directory:
        store = Store(directory)
        store.settings['enabled_agents'] = ['cursor']
        store.settings['notifications'] = False
        store.save()
        env = dict(os.environ, AGENT_METER_DATA_DIR=directory, QT_QPA_PLATFORM='offscreen')
        subprocess.run([str(executable), '--smoke-test'], cwd=directory, env=env,
                       timeout=120, check=True)
        result = json.loads((Path(directory)/'smoke-result.json').read_text())
        if not result or not (Path(directory)/'native-live.png').is_file():
            raise RuntimeError('GUI startup check produced no result')
        print('Packaged GUI startup passed:', json.dumps(result))


if __name__ == '__main__': main()

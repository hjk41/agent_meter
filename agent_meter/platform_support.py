"""Platform paths and login startup without changing global system settings."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

STARTUP_LABEL = 'com.hjk41.agent-meter'


def application_support():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support'
    return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))


def mac_app(name):
    for root in (Path.home() / 'Applications', Path('/Applications')):
        path = root / (name + '.app')
        if path.is_dir():
            return path
    return None


def mac_app_executable(name):
    app = mac_app(name)
    if app:
        try:
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            executable = app / 'Contents/MacOS' / info['CFBundleExecutable']
            if executable.is_file():
                return executable
        except (OSError, ValueError, KeyError):
            pass
    return None


def startup_path():
    return Path.home() / 'Library/LaunchAgents' / (STARTUP_LABEL + '.plist')


def mac_startup_enabled():
    try:
        info = plistlib.loads(startup_path().read_bytes())
        return info.get('Label') == STARTUP_LABEL and info.get('RunAtLoad') is True
    except (OSError, ValueError):
        return False


def mac_startup_arguments():
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        # LaunchServices supplies the normal GUI environment to app bundles.
        if executable.parent.name == 'MacOS' and executable.parent.parent.name == 'Contents':
            return ['/usr/bin/open', '-g', str(executable.parent.parent.parent)]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / 'run.py')]


def set_mac_startup(enabled):
    path = startup_path()
    # A launch-on-login job has no KeepAlive, so Quit continues to work.
    subprocess.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{STARTUP_LABEL}'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(plistlib.dumps({'Label': STARTUP_LABEL,
        'ProgramArguments': mac_startup_arguments(), 'RunAtLoad': True,
        'LimitLoadToSessionType': 'Aqua'}))
    temporary.replace(path)
    subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)

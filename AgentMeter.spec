# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
import PySide6
import sys

# Codex's document runtime adds Poppler/libheif DLL directories to PATH. Those
# provide an ICU build with versioned exports; Qt uses the Windows ICU ABI.
# Keep dependency discovery limited to Python/Qt and the Windows runtime.
qt_dir = Path(PySide6.__file__).parent
system_root = Path(os.environ.get('SystemRoot', 'C:/Windows'))
os.environ['PATH'] = os.pathsep.join(str(p) for p in [Path(sys.executable).parent,
    Path(sys.executable).parent/'DLLs', qt_dir, qt_dir.parent/'shiboken6',
    system_root/'System32', system_root])
a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[('agent_meter/assets/app-icon.png', 'agent_meter/assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Use OS API sets/UCRT/ICU instead of foreign compatibility shims. This filter
# also corrects cached Analysis results from a previously polluted build PATH.
a.binaries = [item for item in a.binaries if not (
    Path(item[0]).name.lower().startswith('api-ms-') or
    Path(item[0]).name.lower() in ('ucrtbase.dll','icuuc.dll','icudt78.dll'))]
pyz = PYZ(a.pure)
dll_diagnose = os.environ.get('AGENT_METER_DLL_DIAG') == '1'
if dll_diagnose:
    a.scripts = [item for item in a.scripts if item[0] != 'run']
    a.scripts.append(('dll_diagnose', str(Path(SPECPATH) / 'tools/dll_diagnose.py'), 'PYSOURCE'))

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AgentMeterDebug' if dll_diagnose else 'AgentMeter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=dll_diagnose,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['agent-meter.ico'],
)

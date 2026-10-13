# Build on macOS; an app bundle includes Python and Qt.
a = Analysis(['run.py'], pathex=[], binaries=[],
    datas=[('agent_meter/assets/app-icon.png', 'agent_meter/assets')],
    hiddenimports=['keyring.backends.macOS'], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='AgentMeter',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='AgentMeter')
app = BUNDLE(coll, name='Agent Meter.app', icon='agent-meter.icns',
    bundle_identifier='com.hjk41.agent-meter', version='0.2.0',
    info_plist={'LSUIElement': True, 'NSHighResolutionCapable': True,
                'CFBundleDisplayName': 'Agent Meter'})

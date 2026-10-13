"""Agent Meter desktop entrypoint. --diagnose collects safe quota summaries."""
import json
import sys
from agent_meter.storage import Store
from agent_meter.model import PROVIDERS
from agent_meter.providers import collect

def diagnose():
    from concurrent.futures import ThreadPoolExecutor
    store=Store()
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs={p:executor.submit(collect,p,store.settings,store.directory) for p in store.settings['order']
              if p in store.settings['enabled_agents']}
        for p,job in jobs.items():
            print(json.dumps(job.result().to_dict(),ensure_ascii=False))

def main():
    if '--diagnose' in sys.argv:
        diagnose(); return
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtCore import QTimer, Qt
    from agent_meter.ui import MeterWindow, app_icon
    app=QApplication(sys.argv); app.setApplicationName('Agent Meter'); app.setOrganizationName('AgentMeter')
    app.setQuitOnLastWindowClosed(False); app.setWindowIcon(app_icon())
    # User-scoped IPC prevents duplicated polling/tray windows.
    import getpass, hashlib
    name='AgentMeter-'+hashlib.sha256(getpass.getuser().encode()).hexdigest()[:12]
    socket=QLocalSocket(); socket.connectToServer(name)
    if socket.waitForConnected(250):
        socket.write(b'show'); socket.flush(); socket.waitForBytesWritten(250); return
    server=QLocalServer(); QLocalServer.removeServer(name)
    if not server.listen(name): return
    smoke='--smoke-test' in sys.argv
    if smoke:
        import tempfile
        import copy
        smoke_directory=tempfile.TemporaryDirectory()
        store=Store(smoke_directory.name)
        store.settings=copy.deepcopy(Store().settings)
        store.settings['notifications']=False
    else: store=Store()
    window=MeterWindow(store)
    if sys.platform=='darwin':
        # Tool windows remain available when another app is active.
        window.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
    window.show()
    if '--claude-login' in sys.argv:
        QTimer.singleShot(200,lambda:window.show_connection('claude'))
    if '--doubao-connection' in sys.argv:
        QTimer.singleShot(200,lambda:window.show_connection('doubao'))
    def activate():
        client=server.nextPendingConnection()
        if client: client.disconnectFromServer(); client.deleteLater()
        window.show(); window.raise_(); window.activateWindow()
    server.newConnection.connect(activate)
    if smoke:
        from pathlib import Path
        def finish_smoke():
            window.grab().save(str(Path.cwd()/'native-live.png'))
            result={p:{'status':s.status,'quotas':len(s.quotas)} for p,s in window.snapshots.items()}
            (Path.cwd()/'smoke-result.json').write_text(json.dumps(result),encoding='utf-8')
            if sys.stdout: print(json.dumps(result),flush=True)
            window.quit()
        QTimer.singleShot(90000,finish_smoke)
        # Exit promptly once the initial collection completes.
        smoke_check=QTimer()
        def check():
            if not window.busy and any(s.collected_at for s in window.snapshots.values()):
                smoke_check.stop(); finish_smoke()
        smoke_check.timeout.connect(check); smoke_check.start(500)
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        from PySide6.QtCore import QAbstractNativeEventFilter
        class HotkeyFilter(QAbstractNativeEventFilter):
            def nativeEventFilter(self,event_type,message):
                if event_type in (b'windows_generic_MSG',b'windows_dispatcher_MSG'):
                    msg=wintypes.MSG.from_address(int(message))
                    if msg.message==0x312 and msg.wParam==0xA17:
                        window.toggle_visible(); return True,0
                return False,0
        hotkey=HotkeyFilter(); app.installNativeEventFilter(hotkey)
        registered=ctypes.windll.user32.RegisterHotKey(None,0xA17,0x0003|0x4000,0x4D)
        if registered: app.aboutToQuit.connect(lambda:ctypes.windll.user32.UnregisterHotKey(None,0xA17))
    elif sys.platform=='darwin':
        from agent_meter.macos_hotkey import MacHotkey
        try:
            hotkey=MacHotkey(window.toggle_visible)
            app.aboutToQuit.connect(hotkey.close)
        except OSError:
            pass  # The menu bar icon is available if the shortcut is occupied.
    app.exec()

if __name__=='__main__':
    main()

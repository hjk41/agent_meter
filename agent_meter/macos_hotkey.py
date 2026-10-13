"""Control + Option + M through Carbon; no keyboard monitoring permission."""
import ctypes
from ctypes import c_uint32, c_void_p, c_int32


class EventType(ctypes.Structure):
    _fields_ = [('event_class', c_uint32), ('kind', c_uint32)]


class HotkeyID(ctypes.Structure):
    _fields_ = [('signature', c_uint32), ('ident', c_uint32)]


class MacHotkey:
    def __init__(self, callback):
        carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
        self.carbon = carbon
        self.handler = c_void_p()
        self.hotkey = c_void_p()
        handler_type = ctypes.CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
        self.callback = handler_type(lambda *_: self._activate(callback))
        carbon.GetApplicationEventTarget.restype = c_void_p
        carbon.InstallEventHandler.argtypes = [c_void_p, handler_type, c_uint32,
            ctypes.POINTER(EventType), c_void_p, ctypes.POINTER(c_void_p)]
        carbon.InstallEventHandler.restype = c_int32
        carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, HotkeyID,
            c_void_p, c_uint32, ctypes.POINTER(c_void_p)]
        carbon.RegisterEventHotKey.restype = c_int32
        carbon.UnregisterEventHotKey.argtypes = [c_void_p]
        carbon.RemoveEventHandler.argtypes = [c_void_p]
        event = EventType(int.from_bytes(b'keyb', 'big'), 6)
        target = carbon.GetApplicationEventTarget()
        status = carbon.InstallEventHandler(target, self.callback, 1, ctypes.byref(event),
            None, ctypes.byref(self.handler))
        if status != 0: raise OSError('Cannot register hotkey handler')
        # kVK_ANSI_M=46, controlKey=4096, optionKey=2048.
        status = carbon.RegisterEventHotKey(46, 4096 | 2048,
            HotkeyID(int.from_bytes(b'AgMt', 'big'), 1), target, 0, ctypes.byref(self.hotkey))
        if status != 0:
            self.close()
            raise OSError('Hotkey is occupied')

    @staticmethod
    def _activate(callback):
        callback()
        return 0

    def close(self):
        if self.hotkey:
            self.carbon.UnregisterEventHotKey(self.hotkey)
            self.hotkey = c_void_p()
        if self.handler:
            self.carbon.RemoveEventHandler(self.handler)
            self.handler = c_void_p()

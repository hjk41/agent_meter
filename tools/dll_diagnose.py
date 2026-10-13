import ctypes
import importlib
import sys
import traceback
for name in ['PySide6.QtCore','PySide6.QtGui','PySide6.QtWidgets']:
    try:
        importlib.import_module(name)
        print(name,'OK',flush=True)
    except Exception:
        traceback.print_exc()
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.GetModuleHandleW.restype=ctypes.c_void_p
k.GetModuleHandleW.argtypes=[ctypes.c_wchar_p]
k.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint]
for name in ['VCRUNTIME140.dll','VCRUNTIME140_1.dll','MSVCP140.dll','MSVCP140_1.dll','MSVCP140_2.dll','Qt6Core.dll','Qt6Gui.dll','Qt6Widgets.dll','pyside6.abi3.dll','shiboken6.abi3.dll']:
    handle=k.GetModuleHandleW(name)
    if handle:
        buf=ctypes.create_unicode_buffer(1000);k.GetModuleFileNameW(handle,buf,1000)
        print(name,buf.value,flush=True)
print('bundle',sys._MEIPASS,flush=True)

"""Export the app artwork to native Windows ICO or macOS ICNS."""
from pathlib import Path
import struct
import sys
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parents[1]

def main():
    source=QImage(str(ROOT/'agent_meter/assets/app-icon.png'))
    if source.isNull(): raise RuntimeError('Application icon artwork is missing')
    if sys.platform == 'darwin':
        chunks = []
        for kind, size in ((b'icp4',16),(b'icp5',32),(b'icp6',64),
                           (b'ic07',128),(b'ic08',256),(b'ic09',512),(b'ic10',1024)):
            image = source.scaled(size,size,Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            data=QByteArray(); buffer=QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer,'PNG'): raise RuntimeError('Icon export failed')
            payload=bytes(data); buffer.close()
            chunks.append(kind+struct.pack('>I',len(payload)+8)+payload)
        payload=b''.join(chunks)
        (ROOT/'agent-meter.icns').write_bytes(b'icns'+struct.pack('>I',len(payload)+8)+payload)
        print('Built macOS application icon: 16–1024 px')
        return
    frames=[]
    for size in (16,20,24,32,40,48,64,128,256):
        image=source.scaled(size,size,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
        data=QByteArray(); buffer=QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer,'PNG'): raise RuntimeError('Icon export failed')
        frames.append((size,bytes(data)))
        buffer.close()
    offset=6+16*len(frames)
    entries=[]
    for size,data in frames:
        entries.append(struct.pack('<BBBBHHII',size%256,size%256,0,0,1,32,len(data),offset))
        offset+=len(data)
    (ROOT/'agent-meter.ico').write_bytes(struct.pack('<HHH',0,1,len(frames))+b''.join(entries)+b''.join(data for _,data in frames))
    print('Built application icon: 9 sizes, 16–256 px')

if __name__=='__main__': main()

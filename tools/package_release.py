"""Package explicit build outputs, preserve macOS symlinks, and verify hashes."""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent_meter import __version__

TARGETS = ('windows-x64','macos-arm64')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def validate_version(tag):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?',tag):
        raise ValueError('Use a version tag such as 0.2.0, without a v prefix')
    if tag != __version__: raise ValueError(f'Tag {tag} does not match source version {__version__}')
    head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    return head


def archive_name(tag,target):
    return f'AgentMeter-{tag}-{target}.zip'


def collect_licenses(destination):
    destination.mkdir(parents=True,exist_ok=True)
    for path in (ROOT/'licenses').iterdir():
        if path.is_file(): shutil.copy2(path,destination/path.name)
    # Include license texts provided by the installed runtime/build wheels.
    versions = {}
    for dist in metadata.distributions():
        name = dist.metadata.get('Name','unknown')
        versions[name] = dist.version
        safe_name = re.sub(r'[^A-Za-z0-9_.-]','_',name+'-'+dist.version)
        for entry in dist.files or []:
            parts = entry.parts
            if not any('license' in p.lower() or p.lower().startswith('copying') for p in parts): continue
            source = Path(dist.locate_file(entry))
            if not source.is_file(): continue
            safe_parts = [p for p in parts if p not in ('.','..') and ':' not in p]
            target = destination/safe_name/Path(*safe_parts)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
    return versions


def package(tag,target,output):
    commit = validate_version(tag)
    expected_os,expected_arch = ('win32','AMD64') if target=='windows-x64' else ('darwin','arm64')
    if sys.platform!=expected_os or platform.machine().lower()!=expected_arch.lower():
        raise ValueError(f'Build {target} on its native platform and architecture')
    build = ROOT/'build'; build.mkdir(exist_ok=True)
    if not build.resolve().is_relative_to(ROOT): raise ValueError('Build directory must remain in the project')
    output.mkdir(parents=True,exist_ok=True)
    archive = output/archive_name(tag,target)
    with tempfile.TemporaryDirectory(prefix='release-stage-',dir=build) as directory:
        stage = Path(directory)/archive.stem; stage.mkdir()
        if target=='windows-x64':
            shutil.copy2(ROOT/'dist/AgentMeter.exe',stage/'AgentMeter.exe')
        else:
            app = ROOT/'dist/Agent Meter.app'
            info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
            if info.get('CFBundleShortVersionString')!=tag: raise ValueError('macOS bundle version mismatch')
            subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True)
            subprocess.run(['/usr/bin/ditto',str(app),str(stage/app.name)],check=True)
        for name in ('README.md','THIRD_PARTY_NOTICES.md'):
            shutil.copy2(ROOT/name,stage/name)
        versions = collect_licenses(stage/'third_party_licenses')
        manifest = {'tag':tag,'version':__version__,'commit':commit,'target':target,
                    'python':platform.python_version(),'dependencies':versions}
        (stage/'BUILD_INFO.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        if target=='macos-arm64':
            subprocess.run(['/usr/bin/ditto','-c','-k','--sequesterRsrc','--keepParent',
                            str(stage),str(archive.resolve())],check=True)
        else:
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(stage.rglob('*')):
                    if path.is_file(): bundle.write(path,path.relative_to(stage.parent).as_posix())
    manifest.update(archive=archive.name,sha256=sha256(archive))
    (output/(archive.name+'.json')).write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(f'Packaged {archive.name} ({archive.stat().st_size} bytes)')


def checksums(tag,output):
    commit = validate_version(tag)
    rows = []
    for target in TARGETS:
        archive = output/archive_name(tag,target)
        manifest = json.loads((output/(archive.name+'.json')).read_text())
        if any(manifest.get(key)!=value for key,value in
               (('tag',tag),('version',__version__),('commit',commit),('target',target),('archive',archive.name))):
            raise ValueError(f'Manifest mismatch: {archive.name}')
        digest = sha256(archive)
        if digest!=manifest.get('sha256'): raise ValueError(f'Hash mismatch: {archive.name}')
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            suffix = '/AgentMeter.exe' if target=='windows-x64' else '/Agent Meter.app/Contents/MacOS/AgentMeter'
            if not any(name.endswith(suffix) for name in names): raise ValueError('Missing application in archive')
            if not any(name.endswith('/BUILD_INFO.json') for name in names): raise ValueError('Missing build information')
        rows.append(f'{digest}  {archive.name}')
    (output/'SHA256SUMS.txt').write_text('\n'.join(rows)+'\n',encoding='utf-8')
    print('Validated both release archives and SHA256 checksums')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag',required=True)
    parser.add_argument('--target',choices=TARGETS)
    parser.add_argument('--output',type=Path,default=ROOT/'release-assets')
    parser.add_argument('--check-version',action='store_true')
    parser.add_argument('--checksums-only',action='store_true')
    args = parser.parse_args()
    if args.check_version: validate_version(args.tag); print('Source version matches tag:',args.tag)
    elif args.checksums_only: checksums(args.tag,args.output)
    elif args.target: package(args.tag,args.target,args.output)
    else: parser.error('Choose --target, --check-version, or --checksums-only')


if __name__=='__main__': main()

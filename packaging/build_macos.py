"""Native Mac build preparation, verified native asset, tests, and app archive.

Run on Apple Silicon via build-macos.command. No notarization or security bypass.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from macos_requirements import mac_requirements

ROOT = Path(__file__).resolve().parents[1]
WBT_URL = 'https://www.whiteboxgeo.com/WBT_Darwin/WhiteboxTools_darwin_m_series.zip'
WBT_SHA256 = 'b0ff9ad48769df68a604c266e249eb4867ce7505fc297f439cf2cefe0c185c8b'
WBT_BINARY_SHA256 = '63ce77174e2abc32590df2b4868b6fb4123d8598a540290ef9669b2cb919647b'
WBT_BINARY = 'WhiteboxTools_darwin_m_series/WBT/whitebox_tools'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_native(path):
    if digest(path) != WBT_SHA256:
        raise ValueError('WhiteboxTools archive checksum mismatch; refusing to execute it.')
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in item.filename:
                raise ValueError('Unsafe archive member')
        binary = archive.read(WBT_BINARY)
    if hashlib.sha256(binary).hexdigest() != WBT_BINARY_SHA256:
        raise ValueError('WhiteboxTools executable checksum mismatch')
    if len(binary) < 8 or struct.unpack('<II', binary[:8]) != (0xFEEDFACF, 0x0100000C):
        raise ValueError('WhiteboxTools is not Mach-O arm64')


def prepare_native():
    target = ROOT / 'native' / 'macos-arm64' / 'whitebox_tools.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        partial = target.with_suffix('.download')
        request = urllib.request.Request(WBT_URL, headers={'User-Agent': 'CNG-macOS-build/0.1'})
        try:
            with urllib.request.urlopen(request, timeout=90) as response, partial.open('wb') as out:
                shutil.copyfileobj(response, out)
            verify_native(partial)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    verify_native(target)
    return target


def collect_licenses(native):
    target = ROOT / '.cache' / 'macos-licenses'
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for dist in metadata.distributions():
        name = dist.metadata['Name']
        records.append({'name': name, 'version': dist.version})
        for item in dist.files or []:
            if not any(word in item.name.lower() for word in ('license', 'copying', 'notice')):
                continue
            source = Path(dist.locate_file(item))
            if source.is_file():
                relative = Path(*[part for part in item.parts if part not in ('..', '.')])
                dest = target / name / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
    for source in [ROOT / 'LICENSE', ROOT / 'vendor' / 'LICENSE-SWMManywhere',
                   ROOT / 'vendor' / 'swmmanywhere-src' / 'netcomp' / 'LICENSE.txt',
                   *list((ROOT / 'web' / 'vendor').glob('LICENSE-*'))]:
        shutil.copy2(source, target / (('netcomp-' if source.parent.name == 'netcomp' else '') + source.name))
    with zipfile.ZipFile(native) as archive:
        for item in archive.infolist():
            if 'license' in item.filename.lower() and not item.is_dir() and '/._' not in item.filename:
                (target / ('WhiteboxTools-' + Path(item.filename).name)).write_bytes(archive.read(item))
    (target / 'dependency-manifest.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    return target


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true', help='Install, verify and test; do not freeze')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or sys.version_info[:2] != (3, 12):
        raise SystemExit('Build requires native Apple Silicon macOS and Python 3.12; cross-compilation is unsupported.')
    if sys.prefix == sys.base_prefix:
        raise SystemExit('Use build-macos.command to create the isolated .venv-macos environment.')
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    constraints = cache / 'requirements-macos-derived.txt'
    constraints.write_text(mac_requirements((ROOT / 'requirements-lock.txt').read_text()), encoding='utf-8')
    run(sys.executable, '-m', 'pip', 'install', '-r', constraints)
    run(sys.executable, '-m', 'pip', 'check')
    native = prepare_native()
    with tempfile.TemporaryDirectory(prefix='cng-wbt-', dir=cache) as folder:
        with zipfile.ZipFile(native) as archive:
            archive.extractall(folder)
        executable = Path(folder) / WBT_BINARY
        executable.chmod(0o755)
        run(executable, '--version', timeout=30)
    run(sys.executable, '-c', 'import bootstrap; import webview.platforms.cocoa; import rasterio, pyogrio, pyproj; import swmm.toolkit.solver; import swmmanywhere; import numba')
    run(sys.executable, '-m', 'pytest', 'tests', '-q')
    frozen = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], cwd=ROOT, text=True)
    (cache / 'requirements-macos-arm64.lock.txt').write_text(frozen, encoding='utf-8')
    license_dir = collect_licenses(native)
    shutil.copy2(cache / 'requirements-macos-arm64.lock.txt', license_dir)
    if args.prepare_only:
        print('Dependencies, native tools and tests passed. GUI and real generation still need Mac validation.')
        return
    stage = cache / 'macos-release-stage'
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', stage,
        '--workpath', cache / 'macos-build', ROOT / 'studio-macos.spec')
    bundle = stage / 'CNG 城市排水管网生成.app'
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', bundle)
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    archive = out / 'CNG-macOS-AppleSilicon-UNVERIFIED.zip'
    run('/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', bundle, archive)
    archive.with_suffix('.zip.sha256').write_text(f'{digest(archive)}  {archive.name}\n', encoding='ascii')
    print(f'Built {archive}. Not notarized or release-validated. Complete README_MAC.md before distributing.')


if __name__ == '__main__':
    main()

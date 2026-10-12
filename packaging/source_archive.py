"""Archive reviewed source files only; never traverse data or release runtimes."""
from pathlib import Path
import hashlib
import json
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {'.git', '.cache', '.uv-cache', '__pycache__', '.pytest_cache', 'data',
           'output', 'cache', 'dist', 'build', 'native', 'sample', '.agents', '.codex'}


def source_files():
    result = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT)
    paths = sorted({name.decode('utf-8') for name in result.split(b'\0') if name})
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or any(
                part in BLOCKED or part.startswith('.venv') for part in relative.parts):
            raise ValueError(f'Non-source path in manifest: {name}')
        if relative.name.startswith('.env') or relative.suffix.lower() in {'.key', '.pem', '.p12', '.pfx', '.zip', '.exe'}:
            raise ValueError(f'Private or binary artifact in manifest: {name}')
        source = ROOT / relative
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT) or not source.is_file():
            raise ValueError(f'Unexpected source path: {name}')
        yield source, relative


def main():
    output = ROOT / 'dist' / 'CNG-macOS-AppleSilicon-Source.zip'
    output.parent.mkdir(exist_ok=True)
    files = list(source_files())
    temporary = output.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, relative in files:
            info = zipfile.ZipInfo.from_file(source, (Path('CNG') / relative).as_posix())
            info.create_system = 3
            info.external_attr = (0o100755 if relative.suffix == '.command' else 0o100644) << 16
            data = source.read_bytes()
            if relative.suffix in {'.command', '.sh'}:
                data = data.replace(b'\r\n', b'\n')
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip():
            raise ValueError('Source archive failed integrity check')
    temporary.replace(output)
    with output.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    output.with_suffix('.zip.sha256').write_text(f'{checksum}  {output.name}\n', encoding='ascii')
    print(json.dumps({'file': str(output), 'files': len(files), 'bytes': output.stat().st_size, 'sha256': checksum}))


if __name__ == '__main__':
    main()

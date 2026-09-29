#!/usr/bin/env python3
"""Image-build-only installer for pinned official toolchains and EROFS readers."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile


def install():
    pins = json.loads(Path('/usr/share/workbench/toolchains.json').read_text())
    for tool, record in [*pins['archives'][platform.machine()].items(), ('erofs-readers', pins['erofs_readers'])]:
        with tempfile.TemporaryDirectory(prefix='workbench-toolchain-') as directory:
            root = Path(directory)
            archive = root / 'download.tar'
            subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '2',
                            '--connect-timeout', '20', '--max-time', '3600', record['url'],
                            '--output', str(archive)], check=True)
            with archive.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                    raise ValueError(f'official {tool} archive checksum mismatch')
            with tarfile.open(archive) as source:
                # Only a pinned, verified toolchain distribution is unpacked.
                source.extractall(root / 'extracted', filter='data')
            directories = list((root / 'extracted').iterdir())
            if len(directories) != 1 or not directories[0].is_dir():
                raise ValueError(f'unexpected {tool} distribution layout')
            if tool == 'go':
                directories[0].rename('/usr/local/go')
            elif tool == 'erofs-readers':
                source = directories[0]
                subprocess.run(['./autogen.sh'], cwd=source, check=True)
                subprocess.run(['./configure', '--disable-lz4', '--disable-lzma', '--without-zlib',
                                '--without-libzstd', '--without-libdeflate', '--without-xxhash',
                                '--without-libcurl', '--without-openssl', '--without-libxml2',
                                '--without-json-c', '--without-libnl3', '--disable-multithreading'],
                               cwd=source, check=True)
                for target in ('lib', 'fsck', 'dump'):
                    subprocess.run(['make', '-C', target, '-j2'], cwd=source, check=True)
                # Readers only; the selected product still owns patched mkfs.erofs.
                for target in ('fsck', 'dump'):
                    shutil.copy2(source / target / (target + '.erofs'), '/usr/local/bin/' + target + '.erofs')
                shutil.copy2(source / 'COPYING', '/usr/share/workbench/erofs-readers.COPYING')
            else:
                subprocess.run(['sh', str(directories[0] / 'install.sh'), '--prefix=/usr/local/rust',
                                '--disable-ldconfig'], check=True)
    subprocess.run(['/usr/local/go/bin/go', 'version'], check=True)
    subprocess.run(['/usr/local/rust/bin/rustc', '--version'], check=True)
    subprocess.run(['/usr/local/rust/bin/cargo', '--version'], check=True)


if __name__ == '__main__':
    install()

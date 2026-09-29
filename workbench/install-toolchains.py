#!/usr/bin/env python3
"""Image-build-only installer for versioned official Go and Rust distributions."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile


def install():
    pins = json.loads(Path('/usr/share/workbench/toolchains.json').read_text())
    for tool, record in pins['archives'][platform.machine()].items():
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
            else:
                subprocess.run(['sh', str(directories[0] / 'install.sh'), '--prefix=/usr/local/rust',
                                '--disable-ldconfig'], check=True)
    subprocess.run(['/usr/local/go/bin/go', 'version'], check=True)
    subprocess.run(['/usr/local/rust/bin/rustc', '--version'], check=True)
    subprocess.run(['/usr/local/rust/bin/cargo', '--version'], check=True)


if __name__ == '__main__':
    install()

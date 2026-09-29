#!/usr/bin/env python3
"""Stream bounded regular diagnostic files; never follow links or mutate outputs."""
import argparse
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile

PER_FILE = 8 * 1024**2
TOTAL = 128 * 1024**2


def collect(root, stream, per_file=PER_FILE, total=TOTAL):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('diagnostics require an existing task root')
    records, remaining = {}, total
    with tarfile.open(fileobj=stream, mode='w|gz') as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            # Only results and owned instance outputs; no product inputs,
            # workspaces, daemon data, secrets from other tasks, or symlinks.
            parts = base.relative_to(root).parts
            if 'output' not in parts:
                dirs[:] = sorted(name for name in dirs if name not in
                                 {'release', 'work', 'home', 'build', 'docker', 'containerd', 'journal', 'inputs'})
            dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
            for name in sorted(files):
                if 'output' not in parts and name not in {'result.json', 'instance.json'}:
                    continue
                path = base / name
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK) if not path.is_symlink() else None
                if fd is None:
                    continue
                with os.fdopen(fd, 'rb') as source:
                    entry = os.fstat(source.fileno())
                    if not stat.S_ISREG(entry.st_mode):
                        continue
                    size = min(entry.st_size, per_file, remaining)
                    relative = str(path.relative_to(root))
                    records[relative] = {'original_size': entry.st_size, 'retained_tail_size': size}
                    if size == 0 and entry.st_size:
                        continue
                    source.seek(entry.st_size - size)
                    data = source.read(size)
                info = tarfile.TarInfo(relative)
                info.size, info.mode = len(data), 0o644
                archive.addfile(info, io.BytesIO(data))
                remaining -= len(data)
        data = (json.dumps(records, sort_keys=True, indent=2) + '\n').encode()
        info = tarfile.TarInfo('diagnostics-index.json')
        info.size, info.mode = len(data), 0o644
        archive.addfile(info, io.BytesIO(data))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    collect(args.root, sys.stdout.buffer)

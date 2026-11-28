#!/usr/bin/env python3
"""Preserve regular artifact files/modes without trusting tar extraction paths."""
import argparse
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from artifacts import relative, require


def extract(archive, destination):
    require(not os.path.lexists(destination), "artifact transport destination must be fresh")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep incomplete output private until all headers, contents and compression
    # trailers have been read. The staging tree shares the destination filesystem.
    with tempfile.TemporaryDirectory(prefix="." + destination.name + "-extract-",
                                     dir=destination.parent) as directory:
        stage = Path(directory) / "payload"
        stage.mkdir()
        with tarfile.open(archive) as source:
            names = set()
            # Do not call getmembers(): that scans a compressed archive to its
            # end and extracting the first file then decompresses it again.
            for member in source:
                name = member.name.rstrip("/")
                relative(name)
                require(name not in names, "duplicate transport path")
                names.add(name)
                require(member.isfile() or member.isdir(), "transport cannot contain links/devices")
                require(member.size >= 0, "invalid transport member size")
                require(member.mode & 0o7022 == 0, "unsafe transport permissions")
                target = stage / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as data, target.open("xb") as output:
                        shutil.copyfileobj(data, output)
                    target.chmod(member.mode & 0o777)
            # Tar iteration stops at its end marker, possibly before a gzip
            # trailer. Finish the same forward stream so truncation/CRC errors
            # cannot publish an apparently complete tree.
            while source.fileobj.read(1024 * 1024):
                pass
        require(not os.path.lexists(destination), "artifact transport destination must be fresh")
        stage.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("extract",))
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    extract(args.archive, args.destination)

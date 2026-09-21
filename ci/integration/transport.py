#!/usr/bin/env python3
"""Preserve regular artifact files/modes without trusting tar extraction paths."""
import argparse
from pathlib import Path
import shutil
import tarfile

from artifacts import relative, require


def extract(archive, destination):
    require(not destination.exists(), "artifact transport destination must be fresh")
    with tarfile.open(archive) as source:
        members = source.getmembers()
        names = set()
        for member in members:
            name = member.name.rstrip("/")
            relative(name)
            require(name not in names, "duplicate transport path")
            names.add(name)
            require(member.isfile() or member.isdir(), "transport cannot contain links/devices")
            require(member.mode & 0o7022 == 0, "unsafe transport permissions")
        destination.mkdir(parents=True)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, target.open("xb") as output:
                    shutil.copyfileobj(data, output)
                target.chmod(member.mode & 0o777)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("extract",))
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    extract(args.archive, args.destination)

#!/usr/bin/env python3
"""Validate a native component's material archive before touching the workspace."""

from pathlib import Path, PurePosixPath
import posixpath
import sys
import tarfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def relative(name):
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts, f"unsafe payload path: {name}")
    require(".git" not in path.parts, f"Git state in native payload: {name}")
    return path


def validate(archive, workspace, roots):
    roots = {relative(name) for name in roots if name}
    require(roots and PurePosixPath(".") not in roots, "empty native output list")
    ancestors = {parent for root in roots for parent in root.parents}
    for root in roots:
        for parent in root.parents:
            require(not (workspace / parent).is_symlink(), f"workspace output parent is a symlink: {parent}")

    def allowed(path):
        return any(path == root or root in path.parents for root in roots)

    with tarfile.open(archive, "r:") as stream:
        members = {}
        for item in stream:
            name = relative(item.name)
            require(name not in members, f"duplicate native payload path: {name}")
            require(allowed(name) or (item.isdir() and name in ancestors),
                    f"unexpected native payload path: {name}")
            require(item.isdir() or item.isfile() or item.issym() or item.islnk(),
                    f"unsupported native payload type: {name}")
            require(not item.mode & 0o6000, f"privileged mode in native payload: {name}")
            if item.issym() or item.islnk():
                require(not PurePosixPath(item.linkname).is_absolute(), f"absolute native payload link: {name}")
                target = str(name.parent / item.linkname) if item.issym() else item.linkname
                target = relative(posixpath.normpath(target))
                require(allowed(target), f"native payload link escapes outputs: {name}")
            members[name] = item
        require(roots <= members.keys(), f"native payload omits outputs: {sorted(roots - members.keys())}")
        for root in roots:
            item = members[root]
            require(not item.isfile() or item.size > 0, f"empty required native material: {root}")
        for name, item in members.items():
            for parent in name.parents:
                require(parent not in members or members[parent].isdir(),
                        f"native payload traverses a link or file: {name}")
            if item.islnk():
                target = relative(posixpath.normpath(item.linkname))
                require(target in members and members[target].isfile(),
                        f"native payload hard link lacks a regular target: {name}")


if __name__ == "__main__":
    try:
        validate(Path(sys.argv[1]), Path(sys.argv[2]), sys.stdin.read().splitlines())
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit(f"native-cache: {error}")

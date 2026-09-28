"""Validate the Demo's locked, prebuilt Python wheel inputs without networking."""
import hashlib
import json
from pathlib import Path
import re


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def lock_records(path):
    require(path.is_file() and not path.is_symlink(), 'missing Demo wheel lock')
    records = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z0-9][a-z0-9-]*)==([A-Za-z0-9.]+)((?: --hash=sha256:[0-9a-f]{64})+)', line)
        require(match is not None, 'Demo lock must contain only exact hash-locked packages')
        name, version, hashes = match.groups()
        require(name not in records, 'duplicate Demo locked package')
        records[name] = {'version': version, 'hashes': set(re.findall(r'[0-9a-f]{64}', hashes))}
    require(records, 'empty Demo wheel lock')
    return records


def validate(directory, lock, requirements, arch):
    require(directory.is_dir() and not directory.is_symlink(), 'missing prepared Demo wheelhouse')
    manifest_path = directory / 'manifest.json'
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'missing Demo wheel manifest')
    manifest = json.loads(manifest_path.read_text())
    require(manifest['arch'] == arch and manifest['python'] == '3.12', 'Demo wheel target mismatch')
    require(manifest['lock_sha256'] == digest(lock) and
            manifest['requirements_sha256'] == digest(requirements), 'Demo wheel source identity mismatch')
    locked = lock_records(lock)
    require(requirements.read_text().strip() == 'e2b==' + locked['e2b']['version'], 'Demo SDK version differs from lock')
    wheels = manifest['wheels']
    require({path.name for path in directory.iterdir()} == {'manifest.json', *wheels}, 'missing or undeclared Demo wheels')
    packages = {}
    for name, record in wheels.items():
        require(re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', name), 'unsafe Demo wheel filename')
        path = directory / name
        require(path.is_file() and not path.is_symlink(), 'invalid Demo wheel file')
        package = record['package']
        require(package in locked and package not in packages and record['version'] == locked[package]['version'],
                'Demo wheel package/version differs from lock')
        actual = digest(path)
        require(actual == record['sha256'] and actual in locked[package]['hashes'], 'Demo wheel checksum mismatch')
        packages[package] = record['version']
    require(set(packages) == set(locked), 'incomplete Demo wheel closure')
    return manifest

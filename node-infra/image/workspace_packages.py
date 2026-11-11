# SPDX-License-Identifier: Apache-2.0
"""Assemble native local products with their owners' source and license material."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
sys.path.insert(0, str(ROOT / 'ci/integration'))
import archive_paths
import artifacts
import go_workspace
import tag_sources


def unit_version(unit, sha):
    """Numeric label for the local producer invocation only; no tag is created."""
    if unit not in artifacts.UNITS or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError(f'invalid workspace identity for {unit}: {sha}')
    prefix = unit + '-' if unit in ('runtime', 'vmlinux') else ''
    return f'{prefix}v0.0.{int(sha[:10], 16)}'


def build_env(sources, units):
    """Select the same local Go modules for compilation and license collection."""
    if set(units) != set(artifacts.UNITS):
        raise ValueError('workspace requires all six product units')
    sources = sources.resolve()
    env = os.environ.copy()
    # Local packagers produce files only. Do not forward publication credentials.
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        env.pop(name, None)
    owners = set()
    for unit in artifacts.UNITS:
        row = units[unit]
        repo = tag_sources.repository(unit)
        owner = repo.split('/')[1]
        source = sources / owner
        sha = row['sha']
        label = unit_version(unit, sha)
        if row['repository'] != repo or row['version'] != 'git:' + sha:
            raise ValueError(f'invalid workspace source selection: {unit}')
        if (tag_sources.git(source, 'tag', '--list', label)
                and tag_sources.git(source, 'rev-parse', f'refs/tags/{label}^{{commit}}') != sha):
            raise ValueError(f'{unit}: existing tag {label} differs from selected commit')
        owners.add(owner)
        if unit in ('accelerator', 'connector', 'sandboxer'):
            prefix = 'RELEASE_' + unit.upper()
            env[prefix + '_SOURCE_DIR'] = str(source)
            env[prefix + '_SOURCE_SHA'] = sha
            env[prefix + '_VERSION'] = label
    # guest-runtime/native-deps may otherwise select a WSL-specific source path
    # invisible to its packager. Use one explicit kernel input for both steps.
    env.setdefault('LINUX_BUILD_SRC', str(sources / 'guest-runtime/native-deps/build/src/linux'))
    env['RELEASE_LINUX_SOURCE_DIR'] = env['LINUX_BUILD_SRC']
    return go_workspace.configure(sources, owners, env)


def stage(sources, staging, arch, units, env=None):
    """Package native build outputs, then merge the six validated owner archives."""
    if arch not in artifacts.ARCHES:
        raise ValueError(f'unsupported workspace architecture: {arch}')
    sources = sources.resolve()
    if env is None:
        env = build_env(sources, units)
    packages = staging.absolute() / 'packages'
    packages.mkdir(parents=True, exist_ok=False)
    archives = []
    for unit in artifacts.UNITS:
        row = units[unit]
        source = sources / tag_sources.repository(unit).split('/')[1]
        kind = [unit] if unit in ('runtime', 'vmlinux') else []
        label = unit_version(unit, row['sha'])
        output = packages / unit
        # Run in the owner root, as its own make release would. The producer
        # derives/checks provenance for the actual compiled bytes and toolchain.
        selected_env = dict(env, RELEASE_SOURCE_ROOT=str(source), SOURCE_SHA=row['sha'],
                            RELEASE_BIN_DIR=str(source / 'bin' / arch),
                            RELEASE_NATIVE_BIN_DIR=str(source / 'native-deps' / 'bin' / arch))
        subprocess.run(['bash', 'scripts/release.sh', 'package', *kind, label, arch, str(output)],
                       cwd=source, env=selected_env, check=True)
        archive = output / 'assets' / artifacts.archive_name(unit, label, arch)
        if not archive.is_file() or archive.is_symlink():
            raise ValueError(f'{unit}: owner packager did not produce {archive}')
        archives.append(archive)
    products = staging.absolute() / 'products'
    archive_paths.extract(archives, products)
    return products

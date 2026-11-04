"""Generate and verify workspaces for explicit selected local Go module closures."""
import argparse
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

DEPENDENCIES = {'sandboxer': ('accelerator', 'connector'),
                'orchestrator': ('sandboxer', 'accelerator', 'connector'),
                'guest-runtime': ('accelerator',), 'accelerator': (), 'connector': ()}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def closure(owners):
    result = set()
    def add(owner):
        require(owner in DEPENDENCIES, 'unknown Go workspace owner: ' + owner)
        if owner not in result:
            result.add(owner)
            for dependency in DEPENDENCIES[owner]:
                add(dependency)
    for owner in owners:
        add(owner)
    require(result, 'empty Go workspace closure')
    return sorted(result)


def go_json(go, args, env):
    return json.loads(subprocess.check_output([go, *args], env={**env, 'GOWORK': 'off'}, text=True))


def verify(path, modules, go, env):
    require(path.is_file() and not path.is_symlink(), 'missing or linked Go workspace')
    document = go_json(go, ['work', 'edit', '-json', str(path)], env)
    uses = document.get('Use') or []
    actual = [(path.parent / row['DiskPath']).resolve() for row in uses]
    require(len(actual) == len(modules) and set(actual) == set(modules) and not document.get('Replace'),
            'Go workspace differs from selected module closure')
    for module in modules:
        require(module.is_dir() and not module.is_symlink() and (module / 'go.mod').is_file()
                and not (module / 'go.mod').is_symlink(), 'missing or linked local Go module: ' + str(module))
        doc = go_json(go, ['-C', str(module), 'mod', 'edit', '-json'], env)
        require(doc.get('Module', {}).get('Path') == 'github.com/kuasar-sandbox/' + module.name,
                'local Go module path differs from selected owner: ' + module.name)


    output = subprocess.check_output([go, 'list', '-m', '-json',
        *('github.com/kuasar-sandbox/' + module.name for module in modules)],
        cwd=modules[0], env={**env, 'GOWORK': str(path)}, text=True)
    rows = []
    decoder = json.JSONDecoder()
    while output.strip():
        row, end = decoder.raw_decode(output.lstrip())
        rows.append(row)
        output = output.lstrip()[end:]
    require(len(rows) == len(modules) and all(row.get('Main') is True and
            row.get('Path') == 'github.com/kuasar-sandbox/' + module.name and
            Path(row.get('Dir', '')).resolve() == module for row, module in zip(rows, modules)),
            'Go resolves a selected module outside the local workspace')


def configure(root, owners, environment=None):
    root = root.resolve()
    env = dict(os.environ if environment is None else environment)
    go = shutil.which('go', path=env.get('PATH'))
    require(go is not None, 'selected Go compiler is missing')
    go = str(Path(go).resolve())
    names = closure(owners)
    modules = [root / owner for owner in names]
    for module in modules:
        require((module / 'go.mod').is_file() and not module.is_symlink(),
                'missing local Go module: ' + module.name)
    directory = root / '.ci' / 'go-workspaces' / '-'.join(names)
    require(not any(parent.is_symlink() for parent in (directory, *directory.parents)),
            'linked Go workspace directory')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'go.work'
    if not path.exists():
        with tempfile.TemporaryDirectory(prefix='init-', dir=directory) as temporary:
            subprocess.run([go, 'work', 'init', *map(str, modules)], cwd=temporary,
                           env={**env, 'GOWORK': 'off'}, check=True)
            candidate = Path(temporary) / 'go.work'
            verify(candidate, modules, go, env)
            # Never overwrite an existing caller workspace.
            with path.open('xb') as output:
                output.write(candidate.read_bytes())
    verify(path, modules, go, env)
    env['GOWORK'] = str(path)
    return env


def configure_release(root, owners, selections, environment=None):
    """Validate local release inputs before any build; persist only go.work."""
    require(len(owners) == 1, 'local release requires exactly one owner')
    root = root.resolve()
    owner = owners[0]
    expected = set(closure(owners)) - {owner}
    selected = {}
    for selection in selections:
        name, separator, tag = selection.partition('=')
        require(separator and name in expected and name not in selected,
                'unknown or duplicate release dependency: ' + selection)
        require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?', tag),
                'invalid release dependency tag: ' + tag)
        selected[name] = tag
    require(set(selected) == expected, 'release dependency tags differ from Go module closure')
    env = dict(os.environ if environment is None else environment)
    for name in [owner, *sorted(expected)]:
        module = root / name
        variable = 'RELEASE_SOURCE_ROOT' if name == owner else 'RELEASE_' + name.upper().replace('-', '_') + '_SOURCE_DIR'
        require(not env.get(variable) or Path(env[variable]).resolve() == module,
                variable + ' differs from selected local source directory')
    for name, tag in selected.items():
        module = root / name
        def git(*arguments):
            return subprocess.check_output(['git', '-C', str(module), *arguments], text=True).strip()
        require(git('rev-parse', 'HEAD') == git('rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'),
                'local release dependency is not at selected tag: ' + name + '=' + tag)
        flags = git('-c', 'core.quotePath=true', 'ls-files', '-v')
        require(not any(re.match(r'^[a-zS] ', row) for row in flags.splitlines()),
                'local release dependency uses assume-unchanged or skip-worktree: ' + name)
        require(not git('status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'),
                'local release dependency has uncommitted files: ' + name)
    return configure(root, owners, env)


def launch(go, workspace, modules, arguments, isolate_external=False):
    if isolate_external and os.environ.get('GOWORK') == 'off' and os.environ.get('GO111MODULE') == 'off':
        os.execve(go, [go, *arguments], dict(os.environ))
    modules = [Path(path) for path in modules]
    directory = Path.cwd()
    if arguments and arguments[0] == '-C':
        require(len(arguments) >= 2, 'missing Go -C directory')
        directory = (directory / arguments[1]).resolve()
    elif arguments and arguments[0].startswith('-C='):
        directory = (directory / arguments[0][3:]).resolve()
    directory = directory.resolve()
    owner = next((parent for parent in (directory, *directory.parents) if (parent / 'go.mod').is_file()), None)
    if owner not in modules and isolate_external and os.environ.get('GOWORK') == 'off':
        os.execve(go, [go, *arguments], dict(os.environ))
    require(owner in modules, 'helper Go invocation is outside its selected local module closure')
    env = dict(os.environ, GOWORK=workspace)
    verify(Path(workspace), modules, go, env)
    os.execve(go, [go, *arguments], env)


def helper_environment(root, owner, environment, *, source_checks=False):
    """Adapt immutable helper recipes that hardcode GOWORK=off, only for local modules."""
    env = configure(root, [owner], environment)
    path = Path(env['GOWORK'])
    modules = [str(root.resolve() / name) for name in closure([owner])]
    go = str(Path(shutil.which('go', path=environment.get('PATH'))).resolve())
    launcher = path.parent / 'selected-go'
    code = ('#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent)) + ')\n'
            'from go_workspace import launch\nlaunch(' + ', '.join(map(repr, (go, str(path), modules))) + ', sys.argv[1:])\n')
    launcher.write_text(code); launcher.chmod(0o755)
    env['KUASAR_E2E_GO'] = str(launcher)
    if source_checks:
        # Historical source checks also call raw go with GOWORK=off. Scope this
        # driver to the owner check; explicitly isolated external fixtures stay off.
        commands = path.parent / 'source-commands'; commands.mkdir(exist_ok=True)
        driver = commands / 'go'
        driver.write_text(code.replace(', sys.argv[1:])', ', sys.argv[1:], isolate_external=True)'))
        driver.chmod(0o755)
        env['PATH'] = str(commands) + os.pathsep + environment['PATH']
    if owner == 'sandboxer':
        # The historical Makefile assigns GO itself; pass its existing command-line override.
        make = shutil.which('make', path=environment.get('PATH'))
        require(make is not None, 'make is required for sandboxer helpers')
        commands = path.parent / 'commands'; commands.mkdir(exist_ok=True)
        wrapper = commands / 'make'
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(make) + ' "$@" ' + shlex.quote('GO=' + str(launcher)) + '\n')
        wrapper.chmod(0o755)
        env['PATH'] = str(commands) + os.pathsep + env['PATH']
    return env


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--owners', nargs='+', choices=sorted(DEPENDENCIES), required=True)
    parser.add_argument('--dependency-tag', action='append', default=None, metavar='OWNER=TAG',
                        help='verify the complete local release dependency selection before building')
    args = parser.parse_args()
    env = (configure(args.root, args.owners) if args.dependency_tag is None else
           configure_release(args.root, args.owners, args.dependency_tag))
    print(env['GOWORK'])

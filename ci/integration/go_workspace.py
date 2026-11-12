"""Generate and verify workspaces for explicit selected local Go module closures."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

import go_module_policy as policy

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


def verify(path, modules, go, env, replacements=None):
    require(path.is_file() and not path.is_symlink(), 'missing or linked Go workspace')
    document = go_json(go, ['work', 'edit', '-json', str(path)], env)
    uses = document.get('Use') or []
    actual = [(path.parent / row['DiskPath']).resolve() for row in uses]
    expected = replacements or {}
    rows = document.get('Replace') or []
    actual_replacements = {(r['Old']['Path'], r['Old'].get('Version')): (path.parent / r['New']['Path']).resolve()
                           for r in rows if not r['New'].get('Version')}
    require(len(actual) == len(modules) and set(actual) == set(modules) and
            len(rows) == len(expected) and actual_replacements == expected,
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


def configure(root, owners, environment=None, *, adapt_requirements=False):
    env = dict(os.environ if environment is None else environment)
    names = closure(owners)
    replacements = {}
    if adapt_requirements:
        # PR builds exercise selected local sources, including unpublished
        # producer revisions. Go can still request a required version's .mod
        # when all sources are in 'use' and a third-party import loads the graph.
        go = shutil.which('go', path=env.get('PATH'))
        require(go is not None, 'selected Go compiler is missing')
        for owner in names:
            file = root / owner / 'go.mod'
            require(file.is_file() and not file.is_symlink() and not file.parent.is_symlink(),
                    'missing or linked local Go module: ' + owner)
            for module, version in policy.managed_requires(file.read_text(), go=go).items():
                dep = module.removeprefix(policy.PREFIX)
                if dep in names:
                    replacements[module, version] = (root / dep).resolve()
    return make_workspace(root, names, env, replacements)


def make_workspace(root, names, env, replacements, *, scope=''):
    root = root.resolve()
    env = dict(env)
    go = shutil.which('go', path=env.get('PATH'))
    require(go is not None, 'selected Go compiler is missing')
    go = str(Path(go).resolve())
    modules = [root / owner for owner in names]
    for module in modules:
        require((module / 'go.mod').is_file() and not module.is_symlink(),
                'missing local Go module: ' + module.name)
    directory = root / '.ci' / 'go-workspaces' / '-'.join(names)
    if replacements or scope:
        key = [scope, sorted((mod, version, str(path)) for (mod, version), path in replacements.items())]
        directory /= hashlib.sha256(json.dumps(key).encode()).hexdigest()
    require(not any(parent.is_symlink() for parent in (directory, *directory.parents)),
            'linked Go workspace directory')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'go.work'
    if not path.exists():
        with tempfile.TemporaryDirectory(prefix='init-', dir=directory) as temporary:
            subprocess.run([go, 'work', 'init', *map(str, modules)], cwd=temporary,
                           env={**env, 'GOWORK': 'off'}, check=True)
            candidate = Path(temporary) / 'go.work'
            if replacements:
                args = [f'-replace={module}@{version}={target}'
                        for (module, version), target in sorted(replacements.items())]
                subprocess.run([go, 'work', 'edit', *args, str(candidate)], cwd=temporary,
                               env={**env, 'GOWORK': 'off'}, check=True)
            verify(candidate, modules, go, env, replacements)
            # Publish a complete workspace even when multiple workers share the inputs.
            try:
                os.link(candidate, path)
            except FileExistsError:
                pass
    verify(path, modules, go, env, replacements)
    env['GOWORK'] = str(path)
    return env


def configure_release(root, owners, selections, environment=None, *, version=None):
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
    bindings = {}
    for name, tag in selected.items():
        module = root / name
        sha = git_output(module, 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}')
        require(git_output(module, 'rev-parse', 'HEAD') == sha,
                'local release dependency is not at selected tag: ' + name + '=' + tag)
        bindings[name] = {'tag': tag, 'sha': sha}
    if version is not None:
        return configure_bound_release(root, owner, version, bindings, env)
    for name in selected:
        require_clean_dependency(root / name)
    return configure(root, owners, env)


def git_output(module, *arguments):
    return subprocess.check_output(['git', '-C', str(module), *arguments], text=True).strip()


def require_clean_dependency(module):
    flags = git_output(module, '-c', 'core.quotePath=true', 'ls-files', '-v')
    require(not any(re.match(r'^[a-zS] ', row) for row in flags.splitlines()),
            'local release dependency uses assume-unchanged or skip-worktree: ' + module.name)
    require(not git_output(module, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'),
            'local release dependency has uncommitted files: ' + module.name)


def validate_build_list(owner, expected, replacements, env, go):
    raw = subprocess.check_output([go, 'list', '-mod=readonly', '-m', '-json', 'all'],
                                 cwd=owner, env=env, text=True)
    decoder = json.JSONDecoder()
    while raw.strip():
        row, size = decoder.raw_decode(raw.lstrip())
        raw = raw.lstrip()[size:]
        path = row['Path']
        if path not in policy.MODULES or row.get('Main') and path == policy.PREFIX + owner.name:
            continue
        version = row.get('Version')
        require(path in expected and version == expected[path],
                f'Go resolves {path}@{version}; expected {expected.get(path)}')
        replacement = row.get('Replace')
        target = replacements.get((path, version))
        require((target is None and replacement is None) or
                (target is not None and replacement and not replacement.get('Version') and
                 Path(replacement['Dir']).resolve() == target),
                'unexpected resolved Go module replacement: ' + path)


def configure_bound_release(root, owner, version, dependencies, environment=None):
    """Bind the Go build to the selected producer commits and the publication channel."""
    root = root.resolve()
    env = dict(os.environ if environment is None else environment)
    go = shutil.which('go', path=env.get('PATH'))
    require(go is not None, 'selected Go compiler is missing')
    _, preview = policy.tag_version('component', version.removeprefix('release-').removeprefix('runtime-'))
    require(set(dependencies) == set(closure([owner])) - {owner},
            'bound dependency set differs from the Go module closure')
    expected = {}
    replacements = {}
    for name, binding in dependencies.items():
        target = root / name
        require(not target.is_symlink() and target.is_dir(), 'missing local Go dependency: ' + name)
        tag, sha = binding['tag'], binding['sha']
        core, is_preview = policy.tag_version(name, tag)
        require(re.fullmatch(r'[0-9a-f]{40}', sha) and git_output(target, 'rev-parse', 'HEAD') == sha,
                'source differs from selected dependency SHA: ' + name)
        require_clean_dependency(target)
        key = policy.PREFIX + name
        expected[key] = core
        if is_preview:
            require(preview, 'Stable release selected Preview dependency: ' + name)
            replacements[key, core] = target
    for name in [owner, *dependencies]:
        directory = root / name
        file = directory / 'go.mod'
        require(not directory.is_symlink() and file.is_file() and not file.is_symlink(),
                'missing or linked local Go module: ' + name)
        module = go_json(go, ['-C', str(directory), 'mod', 'edit', '-json'], env)
        require(module['Module']['Path'] == policy.PREFIX + name, 'unexpected Go module path: ' + name)
        changes = policy.declaration_changes(file.read_text(), expected, go=go)
        require(not changes, f'{name}/go.mod differs from aggregate selection: {changes}')
    if preview:
        env = make_workspace(root, [owner], env, replacements, scope=version)
    else:
        env['GOWORK'] = 'off'
    validate_build_list(root / owner, expected, replacements, env, go)
    return env


def launch(go, workspace, modules, arguments, isolate_external=False, uses=None, replacements=None):
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
    if workspace != 'off':
        verify(Path(workspace), [Path(p) for p in (uses or modules)], go, env,
               {key: Path(value) for key, value in (replacements or {}).items()})
    os.execve(go, [go, *arguments], env)


def helper_environment(root, owner, environment, *, source_checks=False):
    """Adapt immutable helper recipes that hardcode GOWORK=off, only for local modules."""
    env = configure(root, [owner], environment, adapt_requirements=True)
    return helper_launcher(root, owner, env, source_checks=source_checks)


def bound_helper_environment(root, owner, version, dependencies, environment):
    env = configure_bound_release(root, owner, version, dependencies, environment)
    if env['GOWORK'] == 'off':
        go = str(Path(shutil.which('go', path=env.get('PATH'))).resolve())
        env.update(KUASAR_E2E_GO=go, KUASAR_CONSUMER_GO=go)
        return env
    return helper_launcher(root, owner, env)


def helper_launcher(root, owner, environment, *, source_checks=False):
    env = dict(environment)
    path = Path(env['GOWORK'])
    modules = [str(root.resolve() / name) for name in closure([owner])]
    go = str(Path(shutil.which('go', path=environment.get('PATH'))).resolve())
    env['KUASAR_CONSUMER_GO'] = go
    document = go_json(go, ['work', 'edit', '-json', str(path)], env)
    uses = [str((path.parent / row['DiskPath']).resolve()) for row in document['Use']]
    replacements = {(row['Old']['Path'], row['Old']['Version']): str((path.parent / row['New']['Path']).resolve())
                    for row in document.get('Replace') or []}
    launcher = path.parent / 'selected-go'
    code = ('#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent)) + ')\n'
            'from go_workspace import launch\nlaunch(' + ', '.join(map(repr, (go, str(path), modules))) +
            f', sys.argv[1:], uses={uses!r}, replacements={replacements!r})\n')
    launcher.write_text(code); launcher.chmod(0o755)
    env['KUASAR_E2E_GO'] = str(launcher)
    if source_checks:
        # Historical source checks also call raw go with GOWORK=off. Scope this
        # driver to the owner check; explicitly isolated external fixtures stay off.
        commands = path.parent / 'source-commands'; commands.mkdir(exist_ok=True)
        driver = commands / 'go'
        driver.write_text(code.replace('sys.argv[1:], uses=', 'sys.argv[1:], isolate_external=True, uses='))
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
    parser.add_argument('--version', help='owner or aggregate release tag identifying the publication channel')
    parser.add_argument('--bound-release', action='store_true', help='verify the frozen release input identities')
    parser.add_argument('--dependency', action='append', nargs=3, default=[], metavar=('OWNER', 'TAG', 'SHA'))
    parser.add_argument('--source-requirements', action='store_true', help='resolve declarations against selected PR sources')
    args = parser.parse_args()
    if args.bound_release:
        if (not args.version or len(args.owners) != 1 or args.dependency_tag is not None or args.source_requirements
                or len({v[0] for v in args.dependency}) != len(args.dependency)):
            parser.error('bound release requires one owner, a version and unique dependency bindings')
        bindings = {owner: {'tag': tag, 'sha': sha} for owner, tag, sha in args.dependency}
        env = configure_bound_release(args.root, args.owners[0], args.version, bindings)
    elif args.dependency_tag is not None and not args.dependency and not args.source_requirements:
        env = configure_release(args.root, args.owners, args.dependency_tag, version=args.version)
    elif not args.version and not args.dependency and args.dependency_tag is None:
        env = configure(args.root, args.owners, adapt_requirements=args.source_requirements)
    else:
        parser.error('invalid combination of workspace source and release options')
    print(env['GOWORK'])

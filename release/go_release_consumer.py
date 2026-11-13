#!/usr/bin/env python3
"""Build temporary Go consumers using the aggregate's actual release channel."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import go_workspace
import go_module_policy
import tag_sources


def prepare(module, owner, sources, selection, version, environment):
    """Set up a disposable main module containing the actual consumer sources."""
    go = shutil.which('go', path=environment.get('PATH'))
    owner_source = sources / owner
    owner_module = 'github.com/kuasar-sandbox/' + owner
    env = dict(environment, GOWORK='off')
    document = json.loads(subprocess.check_output(
        [go, 'mod', 'edit', '-json', str(owner_source / 'go.mod')], cwd=module, env=env))
    if document['Module']['Path'] != owner_module:
        raise ValueError('selected SDK has a different module path')
    modules = go_workspace.closure([owner], root=sources, go=go)
    selected = go_module_policy.owner_selection(selection)
    tags = {name: selected[name]['version'] for name in modules}
    expected = go_module_policy.expected_versions(
        {go_module_policy.OWNER_UNITS[name]: tag for name, tag in tags.items()})
    (module / 'go.mod').write_text(
        'module example.com/kuasar-release-consumer\n\ngo ' + document['Go'] + '\n\n'
        + f'require {owner_module} {expected[owner_module]}\n')
    sums = owner_source / 'go.sum'
    if sums.is_file():
        shutil.copyfile(sums, module / 'go.sum')
    replacements = {}
    preview = '-preview.' in version
    if preview:
        # The only workspace main module is the consumer. Stable dependencies
        # still resolve normally, even when another SDK in the closure is a Preview.
        subprocess.run([go, 'work', 'init', '.'], cwd=module, env=env, check=True)
        env['GOWORK'] = str(module / 'go.work')
    for name, tag in tags.items():
        tag_sources.validate_tag(go_module_policy.OWNER_UNITS[name], tag)
        if '-preview.' not in tag:
            continue
        if not preview:
            raise ValueError(f'Stable {version} selects Preview {name} {tag}')
        directory = sources / name
        if (directory.is_symlink() or directory.resolve().parent != sources.resolve()
                or tag_sources.git(directory, 'rev-parse', 'HEAD') != selected[name]['sha']):
            raise ValueError(f'{name} differs from selected Preview commit')
        path = 'github.com/kuasar-sandbox/' + name
        core = expected[path]
        # Go embeds the literal replacement in binaries, even with -trimpath.
        # A private, verified relative link keeps identical builds reproducible.
        links = module / '.selected-go-sources'
        if not replacements:
            links.mkdir()  # refuse a preexisting directory or symlink
        link = links / name
        link.symlink_to(directory.resolve(), target_is_directory=True)
        subprocess.run([go, 'work', 'edit', f'-replace={path}@{core}=./{links.name}/{name}'],
                       cwd=module, env=env, check=True)
        replacements[path, core] = directory.resolve()
    if preview:
        # Discover the modules of the packages actually imported by the
        # consumer (including its tests) inside the bound workspace. -e allows
        # Go to report imports before those implicit requirements are recorded.
        imports = subprocess.check_output(
            [go, 'list', '-e', '-f', '{{range .Imports}}{{println .}}{{end}}'
             '{{range .TestImports}}{{println .}}{{end}}{{range .XTestImports}}{{println .}}{{end}}', './...'],
            cwd=module, env=env, text=True).split()
        if imports:
            rows = subprocess.check_output(
                [go, 'list', '-e', '-find', '-f', '{{with .Module}}{{if not .Main}}{{.Path}}@{{.Version}}{{end}}{{end}}',
                 *sorted(set(imports))], cwd=module, env=env, text=True).split()
            if rows:
                subprocess.run([go, 'mod', 'edit', *('-require=' + pin for pin in sorted(set(rows)))],
                               cwd=module, env=env, check=True)
    else:
        subprocess.run([go, 'list', '-mod=mod', '-deps', '-test', './...'], cwd=module, env=env,
                       stdout=subprocess.DEVNULL, check=True)
    go_workspace.validate_build_list(module, expected, replacements, env, go)
    return env


def legacy_platform_environment(work, platform, output, selection, environment):
    """Run the historical platform recipe, binding only its two exact SDK builds."""
    sources = platform.parent
    module = work / 'consumer'
    module.mkdir()
    for fixture in ('xproxy', 'xconductor'):
        shutil.copytree(platform / 'test/e2e/platform/helpers' / fixture, module / fixture)
    env = prepare(module, 'orchestrator', sources, selection, environment['KUASAR_GO_RELEASE_VERSION'], environment)
    go = str(Path(shutil.which('go', path=environment['PATH'])).resolve())
    # Keep the admitted owner recipe and its Go sources intact. In particular
    # its guest, versitygw and workspace discovery commands go to the real Go.
    commands = {}
    for fixture in ('xproxy', 'xconductor'):
        args = ['build', '-p', environment['KUASAR_BUILD_JOBS'], '-trimpath', '-o',
                str(output / ('node-infra-' + fixture))]
        input_file = platform / 'test/e2e/platform/helpers' / fixture / 'main.go'
        original = ('-C', str(sources / 'orchestrator'), *args, str(input_file))
        commands[original] = [go, '-C', str(module), *args, './' + fixture]
    driver = work / 'go'
    driver.write_text('#!/usr/bin/env python3\nimport os, sys\n'
                      f'go = {go!r}\ncommands = {commands!r}\n'
                      'args = tuple(sys.argv[1:])\n'
                      'if args in commands:\n'
                      f'    os.execve(go, commands[args], dict(os.environ, GOWORK={env["GOWORK"]!r}))\n'
                      'os.execv(go, [go, *args])\n')
    driver.chmod(0o755)
    return dict(environment, PATH=str(work) + os.pathsep + environment['PATH'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('fixtures', 'published'))
    parser.add_argument('--version', required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--platform', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    with tempfile.TemporaryDirectory(prefix='aggregate-go-consumer-') as directory:
        root = Path(directory)
        if args.action == 'fixtures':
            if args.platform is None or args.output is None:
                parser.error('fixtures requires --platform and --output')
            for fixture in ('xproxy', 'xconductor'):
                shutil.copytree(args.platform / 'test/e2e/platform/helpers' / fixture, root / fixture)
        else:
            if '-preview.' in args.version:
                parser.error('public module installation requires a Stable aggregate')
            (root / 'consumer.go').write_text('package consumer\nimport _ "github.com/kuasar-sandbox/orchestrator/app/telemetry"\n')
        env = prepare(root, 'orchestrator', args.sources.resolve(), selection, args.version, os.environ)
        if args.action == 'fixtures':
            for fixture in ('xproxy', 'xconductor'):
                subprocess.run(['go', 'build', '-p', env['KUASAR_BUILD_JOBS'], '-trimpath',
                                '-o', str((args.output / ('node-infra-' + fixture)).resolve()), './' + fixture],
                               cwd=root, env=env, check=True)
        else:
            subprocess.run(['go', 'build', '-mod=readonly', '.'], cwd=root, env=env, check=True)
            tag = selection['orchestrator']['version']
            subprocess.run(['go', 'install', 'github.com/kuasar-sandbox/orchestrator/cmd/node-ctl@' + tag],
                           cwd=root, env={**env, 'GOBIN': str(root / 'bin')}, check=True)


if __name__ == '__main__':
    main()

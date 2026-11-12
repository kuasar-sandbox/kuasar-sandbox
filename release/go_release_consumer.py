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
    modules = go_workspace.closure([owner])
    tags = {name: selection[name]['version'] for name in modules}
    expected = go_module_policy.expected_versions(tags)
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
        tag_sources.validate_tag(name, tag)
        if '-preview.' not in tag:
            continue
        if not preview:
            raise ValueError(f'Stable {version} selects Preview {name} {tag}')
        directory = (sources / name).resolve()
        if tag_sources.git(directory, 'rev-parse', 'HEAD') != selection[name]['sha']:
            raise ValueError(f'{name} differs from selected Preview commit')
        path = 'github.com/kuasar-sandbox/' + name
        core = expected[path]
        subprocess.run([go, 'work', 'edit', f'-replace={path}@{core}={directory}'],
                       cwd=module, env=env, check=True)
        replacements[path, core] = directory
    # Populate requirements for the actual fixture packages and their tests.
    # Go forbids -mod=mod in a workspace. Reproduce the exact selected Preview
    # substitutions in a disposable modfile while resolving these imports.
    standalone = dict(env, GOWORK='off')
    list_args = [go, 'list', '-mod=mod', '-deps', '-test']
    if replacements:
        draft = module / 'consumer-resolution.mod'
        draft_sum = draft.with_suffix('.sum')
        shutil.copyfile(module / 'go.mod', draft)
        if (module / 'go.sum').exists():
            shutil.copyfile(module / 'go.sum', draft_sum)
        flags = [f'-replace={path}@{core}={source}' for (path, core), source in replacements.items()]
        subprocess.run([go, 'mod', 'edit', *flags, str(draft)], cwd=module, env=standalone, check=True)
        subprocess.run([*list_args, '-modfile=' + str(draft), './...'], cwd=module, env=standalone,
                       stdout=subprocess.DEVNULL, check=True)
        drops = [f'-dropreplace={path}@{core}' for path, core in replacements]
        subprocess.run([go, 'mod', 'edit', *drops, str(draft)], cwd=module, env=standalone, check=True)
        draft.replace(module / 'go.mod')
        if draft_sum.exists():
            draft_sum.replace(module / 'go.sum')
    else:
        subprocess.run([*list_args, './...'], cwd=module, env=standalone, stdout=subprocess.DEVNULL, check=True)
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

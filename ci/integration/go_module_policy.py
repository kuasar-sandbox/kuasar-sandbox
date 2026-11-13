"""Compare Go module requirements with the aggregate's selected component tags."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


PREFIX = 'github.com/kuasar-sandbox/'
UNITS = {'accelerator': 'accelerator', 'connector': 'connector', 'sandboxer': 'sandboxer',
         'orchestrator': 'orchestrator', 'runtime': 'guest-runtime'}
MODULES = frozenset(PREFIX + owner for owner in UNITS.values())
NUMBER = r'(?:0|[1-9][0-9]*)'
TAG = re.compile(rf'(v{NUMBER}\.{NUMBER}\.{NUMBER})(-preview\.[0-9]{{8}}(?:\.[1-9][0-9]*)?)?')


def tag_version(unit, tag):
    """Return the Go release core and whether this component selection is a Preview."""
    if unit == 'runtime':
        if not isinstance(tag, str) or not tag.startswith('runtime-'):
            raise ValueError(f'invalid {unit} tag: {tag}')
        tag = tag.removeprefix('runtime-')
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        raise ValueError(f'invalid {unit} tag: {tag}')
    return match[1], bool(match[2])


def expected_versions(components, component_next_versions=None):
    # component_next_versions contains optional floors for *new* publications.
    # A published/reused selection, including a Preview, names its own core.
    return {PREFIX + owner: tag_version(unit, components[unit])[0]
            for unit, owner in UNITS.items() if unit in components}


def _edit(go, text, arguments):
    # Pass an explicit modfile outside a module: even a host Go older than
    # the module's go directive can inspect it without downloading a toolchain.
    env = dict(os.environ, GOWORK='off', GOFLAGS='', GOENV='off', GOTOOLCHAIN='local',
               GO111MODULE='on', GOPROXY='off')
    with tempfile.TemporaryDirectory(prefix='aggregate-go-mod-') as tmp:
        path = Path(tmp) / 'selected.mod'
        path.write_text(text)
        result = subprocess.run([go, 'mod', 'edit', *arguments, str(path)], cwd=tmp, env=env,
                                text=True, capture_output=True)
        if result.returncode:
            raise ValueError('invalid selected go.mod: ' + result.stderr.strip())
        return result.stdout if '-json' in arguments else path.read_text()


def _requirements(document):
    out = {}
    for req in document.get('Require') or []:
        if req['Path'] in MODULES:
            if req['Path'] in out:
                raise ValueError('duplicate managed Go requirement: ' + req['Path'])
            out[req['Path']] = req['Version']
    return out


def managed_requires(go_mod_text, *, go='go'):
    return _requirements(json.loads(_edit(go, go_mod_text, ['-json'])))


def declaration_changes(go_mod_text, expected, *, go='go'):
    document = json.loads(_edit(go, go_mod_text, ['-json']))
    for replacement in document.get('Replace') or []:
        if replacement['Old']['Path'] in MODULES or replacement['New']['Path'] in MODULES:
            raise ValueError('component replace overrides selection: ' + replacement['Old']['Path'])
    for excluded in document.get('Exclude') or []:
        if expected.get(excluded['Path']) == excluded['Version']:
            raise ValueError('selected Go module version is excluded: ' + excluded['Path'])
    changes = {}
    for module, current in _requirements(document).items():
        if module not in expected:
            raise ValueError('Go dependency missing from component selection: ' + module)
        if current != expected[module]:
            changes[module] = (current, expected[module])
    return changes


def align_declarations(go_mod_text, expected, *, go='go'):
    changes = declaration_changes(go_mod_text, expected, go=go)
    if not changes:
        return go_mod_text
    return _edit(go, go_mod_text, [f'-require={module}@{new}' for module, (_, new) in changes.items()])

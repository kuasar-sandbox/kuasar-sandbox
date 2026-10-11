"""Derive helper declarations only from already admitted clean source roots."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test/e2e/lib'))
import helper_contract as contract
import tag_sources


def inspect(root, source):
    # Callers supply independently admitted source facts, never declaration fields.
    git = tag_sources.git
    contract.require(root.is_dir() and not root.is_symlink() and (root / '.git').is_dir()
                     and not (root / '.git').is_symlink(), 'invalid helper source root')
    contract.require(Path(git(root, 'rev-parse', '--show-toplevel')).resolve() == root.resolve(),
                     'helper source is not its repository root')
    contract.require(git(root, 'config', '--get', 'remote.origin.url').removesuffix('.git') ==
                     'https://github.com/' + source['repository'], 'helper source repository changed')
    if 'tag' in source:
        unit = 'runtime' if source['repository'].endswith('/guest-runtime') else source['repository'].split('/')[1]
        contract.require(tag_sources.inspect_unit(root, unit, source['tag']) == source, 'helper selected tag identity changed')
    contract.require(git(root, 'rev-parse', 'HEAD') == source['sha'] and
                     git(root, 'rev-parse', 'HEAD^{tree}') == source['tree'], 'helper source identity changed')
    contract.require(not git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', 'HEAD', '--')
                     and not git(root, 'ls-files', '--others'), 'dirty helper source')


def owner(root, source):
    inspect(root, source)
    git = tag_sources.git
    data = contract.regular(root, contract.DECLARATION).read_bytes()
    declaration = contract.declaration(data.decode())
    result = {'source': source, 'declaration': data.decode(), 'declaration_sha256': contract.digest(data),
              'declaration_blob': contract.blob(data), 'recipe_sha256': None, 'recipe_blob': None}
    contract.require(git(root, 'rev-parse', 'HEAD:' + contract.DECLARATION) == result['declaration_blob'],
                     'uncommitted helper declaration')
    if declaration['recipe'] is not None:
        recipe = contract.regular(root, declaration['recipe']).read_bytes()
        result.update(recipe_sha256=contract.digest(recipe), recipe_blob=contract.blob(recipe))
        contract.require(git(root, 'rev-parse', 'HEAD:' + declaration['recipe']) == result['recipe_blob'],
                         'uncommitted helper recipe')
    return result


def discover(roots, sources):
    contract.require(set(roots) == set(sources) == set(contract.OWNERS), 'incomplete helper owner roots')
    result = {'schema': 1, 'owners': {name: owner(roots[name], sources[name]) for name in contract.OWNERS}}
    contract.validate(result)
    return result


def verify_sources(expected, roots):
    contract.validate(expected)
    for name, row in expected['owners'].items():
        if row.get('historical') is True:
            inspect(roots[name], row['source'])
        else:
            contract.require(owner(roots[name], row['source']) == row,
                             'helper source/declaration differs from admitted contract')

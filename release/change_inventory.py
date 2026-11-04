"""Read complete, source-pinned component histories before preparing prose.

Only repository contents access is needed. PR references come from GitHub's
merge/squash commit subjects; commits without those references remain visible.
This is a notes generator, never a product admission or publication check.
"""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

SHA = re.compile(r'[0-9a-f]{40}')
UNITS = {'accelerator', 'connector', 'sandboxer', 'orchestrator', 'runtime', 'vmlinux'}


def require(condition, message):
    if not condition:
        raise ValueError('release change inventory: ' + message)


def repository(unit):
    require(unit in UNITS, 'unknown component')
    return 'kuasar-sandbox/' + ('guest-runtime' if unit in ('runtime', 'vmlinux') else unit)


def escape(text):
    return re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', ' '.join(text.split()))


def commit_row(repo, commit):
    sha = commit['sha']
    require(isinstance(sha, str) and SHA.fullmatch(sha), 'invalid commit identity')
    message = commit['commit']['message']
    require(isinstance(message, str) and message.strip(), 'missing commit title')
    subject, *body = message.splitlines()
    merge = re.fullmatch(r'Merge pull request #(\d+) from .+', subject)
    squash = re.search(r' \(#(\d+)\)$', subject)
    number = merge[1] if merge else squash[1] if squash else None
    if merge:
        subject = next((line.strip() for line in body if line.strip()), subject)
    elif squash:
        subject = subject[:squash.start()]
    links = ([f'[#{number}](https://github.com/{repo}/pull/{number})'] if number else [])
    links.append(f'[commit {sha[:12]}](https://github.com/{repo}/commit/{sha})')
    return f'- {escape(subject)} ({", ".join(links)}).'


def kernel_path(path):
    return isinstance(path, str) and (bool(re.fullmatch(r'native-deps/deps/vmlinux/[^/]+\.config', path))
        or path.startswith('native-deps/deps/linux-patches/'))


class Inventory:
    def __init__(self):
        self.cache = {}

    def pages(self, endpoint):
        if endpoint not in self.cache:
            result = subprocess.run(['gh', 'api', '--paginate', '--slurp', endpoint],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode:
                raise RuntimeError('cannot collect release changes: ' + (result.stderr.strip() or endpoint))
            pages = json.loads(result.stdout)
            require(isinstance(pages, list) and pages, 'missing API pages')
            self.cache[endpoint] = pages
        return self.cache[endpoint]

    def tag_sha(self, repo, tag):
        pages = self.pages(f'repos/{repo}/git/ref/tags/{quote(tag, safe="")}')
        require(len(pages) == 1, 'ambiguous tag response')
        target = pages[0].get('object', {})
        require(target.get('type') == 'commit' and isinstance(target.get('sha'), str)
                and SHA.fullmatch(target['sha']), 'selected tag must identify a commit')
        return target['sha']

    def comparison(self, repo, previous, current):
        pages = self.pages(f'repos/{repo}/compare/{previous}...{current}?per_page=100')
        status, total = pages[0].get('status'), pages[0].get('total_commits')
        require(status in ('ahead', 'behind', 'diverged', 'identical')
                and type(total) is int and total >= 0, 'invalid comparison summary')
        commits = []
        for page in pages:
            require(page.get('status') == status and page.get('total_commits') == total
                    and page.get('base_commit', {}).get('sha') == previous
                    and isinstance(page.get('commits'), list), 'comparison pages changed or are invalid')
            commits.extend(page['commits'])
        identities = [commit.get('sha') for commit in commits]
        require(len(commits) == total and len(set(identities)) == total
                and all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in identities),
                'incomplete or duplicated commit inventory')
        return status, commits

    def affects_kernel(self, repo, sha):
        pages = self.pages(f'repos/{repo}/commits/{sha}?per_page=100')
        files = []
        for page in pages:
            require(page.get('sha') == sha and isinstance(page.get('files'), list),
                    'invalid kernel commit file inventory')
            files.extend(page['files'])
        # GitHub caps this endpoint at 3,000 files. Do not silently claim that
        # a potentially truncated file inventory excludes kernel changes.
        require(len(files) < 3000, 'kernel commit file inventory may be truncated')
        return any(kernel_path(file.get('filename')) or kernel_path(file.get('previous_filename'))
                   for file in files)

    def render(self, unit, previous_tag, current_tag, source_sha=None):
        if previous_tag is None:
            return f'Initial selected version: `{current_tag}`.\n'
        if previous_tag == current_tag:
            return f'Selected version unchanged: `{current_tag}`.\n'
        repo = repository(unit)
        previous = self.tag_sha(repo, previous_tag)
        current = source_sha if source_sha is not None else self.tag_sha(repo, current_tag)
        require(isinstance(current, str) and SHA.fullmatch(current), 'invalid selected source SHA')
        status, commits = self.comparison(repo, previous, current)
        lines = [f'Selected version: `{previous_tag}` → `{current_tag}`.', '']
        if status == 'identical':
            lines += ['The version changed without additional repository commits.', '']
        elif status == 'behind':
            lines += ['The selected version is behind the previous selection; this is a version rollback, not a forward update.', '']
        elif status == 'diverged':
            lines += ['The selected version has diverged from the previous selection. The list shows commits unique to the selected source.', '']
        if unit == 'vmlinux':
            lines += ['Kernel release inputs: `native-deps/deps/vmlinux/*.config` and `native-deps/deps/linux-patches/`.', '']
            commits = [commit for commit in commits if self.affects_kernel(repo, commit['sha'])]
            if not commits and status in ('ahead', 'diverged'):
                lines += ['No commits affecting kernel release inputs are unique to the selected source.', '']
        elif unit == 'runtime':
            lines += ['The following repository history also includes shared guest-runtime and kernel changes; it is not limited to runtime-specific source changes.', '']
        if commits:
            lines += [commit_row(repo, commit) for commit in commits] + ['']
        # The selected release tag may not exist yet. Exact source identities
        # make this comparison usable before component publication, too.
        lines += [f'[Full comparison](https://github.com/{repo}/compare/{previous}...{current})', '']
        return '\n'.join(lines)

"""Read only the public, frozen Workbench manifest and config, never layers."""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = 'ghcr.io/kuasar-sandbox/workbench'
MANIFEST_TYPES = ('application/vnd.docker.distribution.manifest.v2+json',
                  'application/vnd.oci.image.manifest.v1+json')
CONFIG_TYPES = ('application/vnd.docker.container.image.v1+json',
                'application/vnd.oci.image.config.v1+json')
MAX_METADATA_BYTES = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_public(url, headers=None, *, blob=False):
    """Bound metadata reads and never forward registry authorization to a CDN."""
    headers = dict(headers or {})
    for _ in range(4):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.fragment, 'invalid public metadata URL')
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                raw = response.read(MAX_METADATA_BYTES + 1)
                require(len(raw) <= MAX_METADATA_BYTES, 'Workbench metadata exceeds size limit')
                return raw
        except urllib.error.HTTPError as error:
            if blob and error.code in (301, 302, 303, 307, 308) and error.headers.get('Location'):
                url = urllib.parse.urljoin(url, error.headers['Location'])
                headers = {}  # Signed public blob URLs must not receive the bearer token.
                error.close()
                continue
            code = error.code
            error.close()
            raise ValueError('public Workbench metadata HTTP ' + str(code)) from None
        except (urllib.error.URLError, OSError):
            raise ValueError('public Workbench metadata transport failure') from None
    raise ValueError('too many public Workbench metadata redirects')


def registry_metadata(row):
    require(re.fullmatch(re.escape(REGISTRY) + '@sha256:[0-9a-f]{64}', row['reference'])
            and re.fullmatch('sha256:[0-9a-f]{64}', row['image_id']),
            'Workbench metadata requires the frozen public reference')
    # An anonymous, pull-only token; no local Docker or GitHub credentials are read.
    token = json.loads(read_public('https://ghcr.io/token?service=ghcr.io&scope=repository%3Akuasar-sandbox%2Fworkbench%3Apull'))
    require(isinstance(token.get('token'), str) and token['token'], 'missing public registry token')
    headers = {'Authorization': 'Bearer ' + token['token'], 'Accept': ', '.join(MANIFEST_TYPES)}
    manifest_digest = row['reference'].split('@')[1]
    raw = read_public('https://ghcr.io/v2/kuasar-sandbox/workbench/manifests/' + manifest_digest, headers)
    require(digest(raw) == manifest_digest, 'Workbench manifest digest mismatch')
    manifest = json.loads(raw)
    require(manifest.get('config', {}).get('digest') == row['image_id'],
            'Workbench manifest/config binding mismatch')
    config = read_public('https://ghcr.io/v2/kuasar-sandbox/workbench/blobs/' + row['image_id'], headers, blob=True)
    return raw, config


def check_manifest_config(row, image, raw, config):
    """Bind the Docker execution ID to the exact admitted config through its manifest."""
    manifest_digest = row['reference'].split('@')[1]
    require(digest(raw) == manifest_digest, 'Workbench manifest digest mismatch')
    manifest, parsed = json.loads(raw), json.loads(config)
    require(manifest.get('schemaVersion') == 2 and manifest.get('mediaType') in MANIFEST_TYPES
            and isinstance(manifest.get('layers'), list) and 'manifests' not in manifest,
            'Workbench requires one native image manifest')
    descriptor = manifest.get('config', {})
    require(descriptor.get('digest') == row['image_id'] == digest(config)
            and type(descriptor.get('size')) is int and descriptor['size'] == len(config)
            and descriptor.get('mediaType') in CONFIG_TYPES, 'Workbench manifest/config binding mismatch')
    local = image.get('Descriptor') or {}
    require(image.get('Id') == local.get('digest') == manifest_digest
            and local.get('mediaType') == manifest['mediaType']
            and type(local.get('size')) is int and local['size'] == len(raw),
            'Workbench local manifest descriptor mismatch')
    require(parsed.get('os') == image['Os'] and parsed.get('architecture') == image['Architecture']
            and (parsed.get('config', {}).get('Labels') or {}) == (image['Config'].get('Labels') or {})
            and parsed.get('rootfs', {}).get('type') == image.get('RootFS', {}).get('Type') == 'layers'
            and parsed.get('rootfs', {}).get('diff_ids') == (image.get('RootFS', {}).get('Layers') or [])
            and len(manifest['layers']) == len(parsed['rootfs']['diff_ids']),
            'Workbench local config/platform/source/layers mismatch')
    return image['Id']

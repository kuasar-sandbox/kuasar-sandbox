"""Prepare a COPY-only customer image from the selected image and prebuilt SDK fixtures."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def prepare(base, tag, helpers):
    with tempfile.TemporaryDirectory(prefix='node-infra-copy-') as directory:
        context = Path(directory)
        root = context / 'rootfs'
        for name in ('xproxy', 'xconductor'):
            target = root / 'opt/node-infra/extensions' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(helpers / ('node-infra-' + name), target)
            target.chmod(0o755)
        for role in ('conductor', 'proxy'):
            config = root / ('etc/node-' + role) / 'conf.d/60-customer-copy.yaml'
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({'paths': {role + '_executable': '/opt/node-infra/extensions/x' + role}}) + '\n')
            unit = root / 'etc/systemd/system' / ('node-' + role + '.service.d/20-copy.conf')
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text('[Service]\nEnvironmentFile=/etc/node-infra/extensions/service.env\n')
        private = root / 'etc/node-infra/extensions'
        private.mkdir(parents=True)
        private.chmod(0o700)
        for name, value in {'marker': 'node-infra-customer-copy\n',
                            'service.env': 'NODE_INFRA_TEST_CONFIG=/etc/node-infra/extensions/marker\n'}.items():
            path = private / name
            path.write_text(value)
            path.chmod(0o600)
        (context / 'Dockerfile').write_text('ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY --chown=0:0 rootfs/ /\n')
        subprocess.run(['docker', 'build', '--pull=false', '--network=none',
                        '--build-arg', 'BASE_IMAGE=' + base, '--tag', tag, str(context)], check=True)


if __name__ == '__main__':
    prepare(sys.argv[1], sys.argv[2], Path(sys.argv[3]))

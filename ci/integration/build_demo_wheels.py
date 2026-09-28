"""Fetch the locked Demo wheel closure only in source/helper build jobs."""
from email.parser import BytesParser
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

spec = importlib.util.spec_from_file_location('demo_wheels', Path(__file__).resolve().parents[2] / 'test/e2e/lib/demo_wheels.py')
wheels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheels)


def build(demo, arch, output):
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        wheels.require(not os.environ.get(key), f'wheel preparation must not receive {key}')
    lock, requirements = demo / 'requirements.lock', demo / 'requirements.txt'
    wheels.lock_records(lock)
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-input',
                    '--no-cache-dir', 'download', '--only-binary=:all:', '--require-hashes',
                    '--python-version', '3.12', '--implementation', 'cp', '--abi', 'cp312',
                    '--platform', 'manylinux2014_' + arch, '--platform', 'manylinux_2_28_' + arch,
                    '--dest', str(output), '--requirement', str(lock)], check=True)
    records = {}
    for path in sorted(output.iterdir()):
        wheels.require(path.is_file() and path.suffix == '.whl', 'unexpected Demo download')
        with zipfile.ZipFile(path) as archive:
            metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            wheels.require(len(metadata) == 1, 'invalid Demo wheel metadata')
            package = BytesParser().parsebytes(archive.read(metadata[0]))
        records[path.name] = {'package': re.sub(r'[-_.]+', '-', package['Name']).lower(),
                              'version': package['Version'], 'sha256': wheels.digest(path)}
    manifest = {'arch': arch, 'python': '3.12', 'lock_sha256': wheels.digest(lock),
                'requirements_sha256': wheels.digest(requirements), 'wheels': records}
    (output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
    wheels.validate(output, lock, requirements, arch)

'use strict';
// Node actions receive the runner's same-run artifact identity. A shell or
// composite action does not. Never discover credentials in runner files or install packages.
const {spawn} = require('node:child_process');
const path = require('node:path');
const child = spawn('python3', ['-B', path.resolve(__dirname,
  '../../../ci/integration/download_artifact.py')], {stdio: 'inherit'});
let canceled = false;
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => { canceled = true; child.kill(signal); });
}
child.on('error', () => { console.error('artifact: supervisor_start_failed'); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = canceled ? 130 : (signal ? 1 : (code ?? 1)); });

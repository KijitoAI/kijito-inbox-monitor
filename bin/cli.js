#!/usr/bin/env node
'use strict';
// kijito-inbox-monitor on npm is a SIGNPOST, not an installer.
//
// The real tool is a pure-Python package on PyPI. Building a Node installer for a
// Python tool is fragile (it would need Node AND Python AND pipx all on PATH), so
// this package does not install anything. It only:
//   1. delegates to the Python tool via uvx / pipx run if either is present, or
//   2. prints how to install it and exits non-zero.
// There is deliberately no postinstall hook (npm v12 disables install lifecycle
// scripts by default, and that hook is the supply-chain-worm pattern).

const { spawnSync } = require('node:child_process');

function present(cmd) {
  const r = spawnSync(cmd, ['--version'], { stdio: 'ignore' });
  return !r.error && (r.status === 0 || r.status === null);
}

const args = process.argv.slice(2);

// Prefer uvx (fast), then `pipx run`. Both run the published PyPI package without a
// persistent install, which is the right zero-friction path for a one-off invocation.
for (const [cmd, prefix] of [['uvx', []], ['pipx', ['run']]]) {
  if (present(cmd)) {
    const r = spawnSync(cmd, [...prefix, 'kijito-inbox-monitor', ...args], {
      stdio: 'inherit',
      env: process.env,
    });
    process.exit(r.status ?? 1);
  }
}

process.stderr.write(
  'kijito-inbox-monitor is a Python tool; this npm package is only a pointer to it.\n' +
  'Install the real tool with one of:\n' +
  '  pipx install kijito-inbox-monitor\n' +
  '  uv tool install kijito-inbox-monitor\n' +
  '  pip install kijito-inbox-monitor\n' +
  'then run:  kijito-inbox-monitor --help\n' +
  'Docs: https://github.com/KijitoAI/kijito-inbox-monitor\n'
);
process.exit(1);

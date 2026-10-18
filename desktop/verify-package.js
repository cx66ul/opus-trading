'use strict';
const fs = require('fs');
const path = require('path');
const asar = require('@electron/asar');

module.exports = async ({ appOutDir }) => {
  const resources = path.join(appOutDir, 'resources');
  const archive = path.join(resources, 'app.asar');
  for (const name of ['main.js', 'backend.js', 'wizard.js', 'preload.js',
    'shell.html', 'shell.css', 'shell.js', 'assets/icon.png', 'assets/tray.png']) {
    if (!asar.extractFile(archive, name).length) throw new Error('Empty packaged file: ' + name);
  }
  for (const name of ['runtime/python.exe', 'runtime/python313.dll',
    'runtime/Lib/encodings/__init__.py', 'app/backend/api/webapi.py',
    'app/backend/web/index.html', 'app/backend/web/app.js', 'app/backend/web/styles.css',
    'app/scripts/daily_recommend.py', 'app/data/hawk.duckdb', 'app/data/hawk.sqlite']) {
    if (!fs.statSync(path.join(resources, name)).size) throw new Error('Empty resource: ' + name);
  }
  if (fs.existsSync(path.join(resources, 'runtime', 'pyvenv.cfg'))) {
    throw new Error('A development venv is not a standalone Python runtime');
  }
  console.log('Verified packaged startup UI, backend, databases, and standalone Python.');
};

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { BackendManager, httpProbe } = require('../backend');
async function server(handler) {
  const s = http.createServer(handler);
  await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  return s;
}
test('readiness rejects 404, HTML, wrong service, and wrong project', async () => {
  let response = { status: 404, body: '{}' };
  const s = await server((_req, res) => { res.writeHead(response.status); res.end(response.body); });
  const url = 'http://127.0.0.1:' + s.address().port + '/api/ready';
  try {
    for (const r of [
      { status: 404, body: '{}' }, { status: 200, body: '<html>other app</html>' },
      { status: 200, body: '{"service":"other","ready":true}' },
      { status: 200, body: JSON.stringify({ service: 'hawk-webapi', ready: true, root: os.tmpdir() }) },
    ]) {
      response = r;
      assert.equal(await httpProbe(url, 500, __dirname), false);
    }
    response = { status: 200, body: JSON.stringify({ service: 'hawk-webapi', ready: true, root: __dirname }) };
    assert.equal(await httpProbe(url, 500, __dirname), true);
  } finally { await new Promise(resolve => s.close(resolve)); }
});
test('unrelated busy port fails promptly without spawning Python', async () => {
  const s = await server((_req, res) => { res.writeHead(404); res.end('{}'); });
  try {
    const be = new BackendManager({ port: s.address().port, root: __dirname, pythonExe: 'missing.exe' });
    assert.equal((await be.start()).ok, false);
    assert.match(be.lastError, /端口/);
    assert.equal(be.proc, null);
  } finally { await new Promise(resolve => s.close(resolve)); }
});
test('hung HTTP body has a bounded readiness timeout', async () => {
  const s = await server((_req, res) => { res.writeHead(200); res.write('{'); });
  try {
    const start = Date.now();
    assert.equal(await httpProbe('http://127.0.0.1:' + s.address().port, 100), false);
    assert.ok(Date.now() - start < 1500);
  } finally { s.closeAllConnections(); await new Promise(resolve => s.close(resolve)); }
});
test('concurrent startup shares its result; stop frees port before restart', async () => {
  const python = process.env.HAWK_TEST_PYTHON;
  assert.ok(python && fs.existsSync(python), 'Set HAWK_TEST_PYTHON to the bundled interpreter');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-start-test-'));
  fs.mkdirSync(path.join(root, 'backend', 'api'), { recursive: true });
  fs.writeFileSync(path.join(root, 'backend', 'api', 'webapi.py'),
    'from fastapi import FastAPI\nfrom pathlib import Path\napp=FastAPI()\n@app.get("/api/ready")\ndef ready():\n return {"service":"hawk-webapi","ready":True,"root":str(Path(__file__).resolve().parents[2])}\n');
  const s = await server(() => {});
  const port = s.address().port;
  await new Promise(resolve => s.close(resolve));
  const be = new BackendManager({ port, root, pythonExe: python, timeoutMs: 15000 });
  try {
    const first = be.start(), second = be.start();
    assert.equal(first, second);
    assert.equal((await first).ok, true);
    const pid = be.proc.pid;
    await be.stop();
    assert.equal(be.state, 'stopped');
    assert.equal(await httpProbe(be.baseUrl + '/api/ready', 150), false);
    assert.equal((await be.start()).ok, true);
    assert.notEqual(be.proc.pid, pid);
    await new Promise(resolve => setTimeout(resolve, 200));
    assert.equal(be.state, 'running');
  } finally {
    await be.stop();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
test('failed child preserves failed state and diagnostics', async () => {
  const s = await server(() => {});
  const port = s.address().port;
  await new Promise(resolve => s.close(resolve));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-fail-test-'));
  const be = new BackendManager({ port, root, pythonExe: process.env.HAWK_TEST_PYTHON, timeoutMs: 10000 });
  try {
    const result = await be.start();
    assert.equal(result.ok, false);
    assert.equal(be.state, 'failed');
    assert.match(be.lastError, /Python/);
    assert.ok(be.stderrTail.length);
  } finally { await be.stop(); fs.rmSync(root, { recursive: true, force: true }); }
});

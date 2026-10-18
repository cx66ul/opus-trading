'use strict';
const { spawn } = require('child_process');
const { EventEmitter } = require('events');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');
const HOST = '127.0.0.1';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const normalPath = p => {
  const value = path.resolve(p);
  return process.platform === 'win32' ? value.toLowerCase() : value;
};

// A successful HTTP code alone does not identify this application's backend.
// Readiness does no database scans, so stale/locked data cannot block the window.
function httpProbe(url, timeoutMs = 800, expectedRoot) {
  return new Promise(resolve => {
    let settled = false;
    let timer;
    const done = value => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const req = http.get(url, res => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', chunk => {
        data += chunk;
        if (data.length > 16384) { done(false); req.destroy(); }
      });
      res.on('error', () => done(false));
      res.on('aborted', () => done(false));
      res.on('end', () => {
        try {
          const body = JSON.parse(data);
          done(res.statusCode === 200 && body.service === 'hawk-webapi' && body.ready === true &&
            (!expectedRoot || (typeof body.root === 'string' && normalPath(body.root) === normalPath(expectedRoot))));
        } catch { done(false); }
      });
    });
    timer = setTimeout(() => { done(false); req.destroy(); }, timeoutMs);
    req.on('error', () => done(false));
  });
}
function portBusy(port) {
  return new Promise(resolve => {
    let settled = false;
    const s = net.createConnection({ host: HOST, port });
    const timer = setTimeout(() => done(false), 800);
    function done(value) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      s.destroy();
      resolve(value);
    }
    s.on('connect', () => done(true));
    s.on('error', () => done(false));
  });
}
class BackendManager extends EventEmitter {
  constructor(opts) {
    super();
    this.port = opts.port || 8870;
    this.root = opts.root;
    this.pythonExe = opts.pythonExe;
    this.dev = !!opts.dev;
    this.timeoutMs = opts.timeoutMs || 45000;
    this.proc = null;
    this.state = 'idle';
    this.lastError = '';
    this.stderrTail = [];
    this._starting = null;
    this._stopping = null;
  }
  get baseUrl() { return `http://${HOST}:${this.port}`; }
  _state(value) { this.state = value; this.emit('status', this.status()); }
  start() {
    if (this._starting) return this._starting;
    if (this._stopping) return this._stopping.then(() => this.start());
    if (this.state === 'running') return Promise.resolve({ ok: true, reused: true, message: '已在运行' });
    this._state('starting');
    this._starting = this._start().catch(async e => {
      await this._dispose();
      return this._fail(e.message);
    }).finally(() => { this._starting = null; });
    return this._starting;
  }
  _fail(message) {
    this.lastError = message;
    this._state('failed');
    return { ok: false, reused: false, message };
  }
  async _start() {
    this.lastError = '';
    this.stderrTail = [];
    if (await httpProbe(this.baseUrl + '/api/ready', 800, this.root)) {
      this._state('running');
      return { ok: true, reused: true, message: '复用本项目已就绪的服务' };
    }
    if (await portBusy(this.port)) {
      return this._fail(`端口 ${this.port} 已被占用，但不是本项目已就绪的服务。请关闭冲突的旧版本后重试。`);
    }
    if (!fs.existsSync(this.root)) return this._fail('项目根目录不存在：' + this.root);
    if (!fs.existsSync(this.pythonExe)) return this._fail('找不到 Python 解释器：' + this.pythonExe + '，请修复安装。');
    const child = spawn(this.pythonExe, ['-m', 'uvicorn', 'backend.api.webapi:app',
      '--host', HOST, '--port', String(this.port), '--log-level', 'warning'], {
      cwd: this.root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONPATH: this.root, PYTHONUNBUFFERED: '1', HAWK_ROOT: this.root },
    });
    this.proc = child;
    child.stdout.on('data', b => this._tail(b.toString()));
    child.stderr.on('data', b => this._tail(b.toString()));
    child.on('error', e => {
      if (this.proc === child) this._fail('Python 启动失败：' + e.message);
    });
    child.on('exit', (code, signal) => {
      if (this.proc !== child) return;
      this.proc = null;
      this._fail(`Python 后端意外退出（code=${code} signal=${signal || '-'}）：${this.stderrTail.slice(-3).join(' | ')}`);
    });
    const deadline = Date.now() + this.timeoutMs;
    while (Date.now() < deadline) {
      if (this.state === 'failed') {
        await this._dispose();
        return { ok: false, reused: false, message: this.lastError };
      }
      if (await httpProbe(this.baseUrl + '/api/ready', 800, this.root)) {
        if (this.proc !== child || this.state === 'failed') continue;
        this._state('running');
        return { ok: true, reused: false, message: '本地服务已就绪' };
      }
      await delay(150);
    }
    await this._dispose();
    return this._fail(`启动超时（${this.timeoutMs / 1000}s）。${this.stderrTail.slice(-3).join(' | ')}`);
  }
  _tail(s) {
    for (const line of s.split(/\r?\n/)) {
      if (line.trim()) this.stderrTail.push(line.trim());
    }
    this.stderrTail = this.stderrTail.slice(-50);
  }
  async _dispose() {
    const child = this.proc;
    if (!child) return;
    // Detach the specific child from state before its exit event can race a restart.
    this.proc = null;
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
    if (process.platform === 'win32') {
      await new Promise((resolve, reject) => {
        const killer = spawn('taskkill', ['/pid', String(child.pid), '/f', '/t'],
          { windowsHide: true, stdio: 'ignore' });
        killer.once('error', reject);
        killer.once('close', code => {
          if (code === 0 || child.exitCode !== null || child.signalCode !== null) resolve();
          else reject(new Error('无法停止 Python 进程 ' + child.pid));
        });
      });
    } else child.kill('SIGTERM');
    for (let i = 0; i < 100; i++) {
      if (child.exitCode !== null || child.signalCode !== null) return;
      await delay(50);
    }
    throw new Error('等待 Python 退出超时：' + child.pid);
  }
  stop() {
    if (this._stopping) return this._stopping;
    this._stopping = (async () => {
      if (this._starting) await this._starting;
      await this._dispose();
      this._state('stopped');
    })().finally(() => { this._stopping = null; });
    return this._stopping;
  }
  status() {
    return { state: this.state, base_url: this.baseUrl,
      pid: this.proc ? this.proc.pid : null, python: this.pythonExe,
      root: this.root, dev: this.dev, last_error: this.lastError,
      stderr_tail: this.stderrTail.slice(-5) };
  }
}
module.exports = { BackendManager, httpProbe, portBusy };

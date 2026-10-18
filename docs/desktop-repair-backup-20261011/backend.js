'use strict';
/**
 * Hawk 桌面端 · Python 后端进程管理
 * ====================================================
 *
 * ★★ 为什么单独成文件，而不是塞进 main.js
 * ---------------------------------------------
 * 桌面端最容易崩的地方不是渲染，而是**后端进程的生死**：
 *   · 用户关窗 ≠ 服务该停（最小化到托盘时应继续跑）
 *   · 服务崩了窗口还开着，用户点任何按钮都是「无响应」
 *   · 端口被占（上一次没退干净）导致新进程起不来
 *   · 起服务时忘了清理上次残留的孤儿进程
 * 这几件事的错误信息全是「白屏」或「一直转圈」，
 * 所以必须在这里集中处理，并在**失败时给出可执行的下一步**。
 *
 * ★ 设计约定
 *   1. **不用 detached**：子进程与主进程同生命周期，
 *      主进程退出时 Node 会终止它 ⇒ 不留孤儿。
 *   2. **端口冲突先查后起**：起之前先探端口，
 *      若已被本项目的健康端点占用 ⇒ 复用，不再起第二个。
 *   3. **启动有超时**：起不来要说「超时」并给出 stderr 尾部，
 *      而不是永远停在「启动中」。
 *   4. **退出必须清场**：无论正常退出还是异常，都尝试终止子进程。
 */

const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');

const HOST = '127.0.0.1';
const START_TIMEOUT_MS = 45000;   // 45s：含 DuckDB 打开 + FastAPI 起停
const PROBE_INTERVAL_MS = 400;

/** 极简 HTTP 探测（不用 fetch —— 主进程 Node 版本不保证有）。 */
function httpProbe(url, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

/** 端口是否被占用（用于区分「服务已起」与「被别的进程占了」）。 */
function portBusy(port) {
  return new Promise((resolve) => {
    const s = net.createConnection({ host: HOST, port });
    s.on('connect', () => { s.destroy(); resolve(true); });
    s.on('error', () => resolve(false));
    setTimeout(() => { s.destroy(); resolve(false); }, 1500);
  });
}

class BackendManager {
  /**
   * @param {object} opts
   * @param {number} opts.port
   * @param {string} opts.root        项目根（含 backend/ 与 data/）
   * @param {string} opts.pythonExe  Python 可执行文件
   * @param {boolean} opts.dev
   */
  constructor(opts) {
    this.port = opts.port || 8870;
    this.root = opts.root;
    this.pythonExe = opts.pythonExe;
    this.dev = !!opts.dev;
    this.proc = null;
    this.state = 'idle';        // idle|starting|running|stopped|failed
    this.lastError = '';
    this.stderrTail = [];
    this.stopping = false;
  }

  get baseUrl() { return `http://${HOST}:${this.port}`; }

  /** 启动。返回 {ok, reused, message}。 */
  async start() {
    if (this.state === 'running' || this.state === 'starting') {
      return { ok: true, reused: true, message: '已在运行' };
    }

    // ── ① 先探端口：可能上一轮已经起来了
    //    ★ 不查直接起 ⇒ 端口冲突抛 EADDRINUSE，
    //      而用户看到的只是「启动失败」，根本不知道该关掉谁。
    if (await httpProbe(`${this.baseUrl}/api/health`)) {
      this.state = 'running';
      return { ok: true, reused: true,
               message: `已有实例在 ${this.baseUrl}，直接复用` };
    }
    const busy = await portBusy(this.port);
    if (busy) {
      // 端口被占但健康端点不通 ⇒ 是别的程序占了端口
      this.state = 'failed';
      this.lastError =
        `端口 ${this.port} 被其它进程占用，且不是 Hawk 服务。`
        + `请在任务管理器结束占用进程，或用 --port 指定其它端口。`;
      return { ok: false, reused: false, message: this.lastError };
    }

    // ── ② 起进程
    //★★★ 先校验项目根目录
    //   实测坑（2026-10-11）：`cwd` 指向**不存在**的目录时，
    //   Windows 上 spawn 抛的是 **ENOENT**，
    //   而 ENOENT 的标准含义是「**可执行文件**找不到」
    //   ⇒ 错误信息变成「Python 进程启动失败：spawn python.exe ENOENT」，
    //   把「项目根目录不存在」误报成「Python 没装」。
    //   用户会照着提示去装 Python，而真正该修的是路径。
    if (!fs.existsSync(this.root)) {
      this.state = 'failed';
      this.lastError =
        `项目根目录不存在：${this.root}`
        + `（这通常意味着安装包不完整，或数据目录被移动）`;
      return { ok: false, reused: false, message: this.lastError };
    }
    if (!fs.existsSync(this.pythonExe)) {
      this.state = 'failed';
      this.lastError =
        `找不到 Python 解释器：${this.pythonExe}`
        + `（安装包可能缺少 runtime 目录）`;
      return { ok: false, reused: false, message: this.lastError };
    }

    this.state = 'starting';
    this.lastError = '';
    this.stderrTail = [];
    const args = ['-m', 'uvicorn', 'backend.api.webapi:app',
                  '--host', HOST, '--port', String(this.port),
                  '--log-level', 'warning'];
    const env = Object.assign({}, process.env, {
      PYTHONPATH: this.root,
      PYTHONUNBUFFERED: '1',
      // ★ 数据目录指到项目内，避免污染用户主目录
      HAWK_ROOT: this.root,
    });

    let child;
    try {
      child = spawn(this.pythonExe, args, {
        cwd: this.root,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (e) {
      this.state = 'failed';
      this.lastError = `无法启动 Python：${e.message}`;
      return { ok: false, reused: false, message: this.lastError };
    }
    this.proc = child;

    child.stdout.on('data', (b) => this._tail(b.toString()));
    child.stderr.on('data', (b) => this._tail(b.toString()));

    child.on('error', (e) => {
      // 常见：Python 不存在（ENOENT）、权限（EACCES）
      this.state = 'failed';
      this.lastError =
        `Python 进程启动失败：${e.message}（可执行文件：${this.pythonExe}）`;
    });

    child.on('exit', (code, signal) => {
      this.proc = null;
      // ★ 主动 stop 时的 exit 是预期结果，不能报成崩溃
      if (this.stopping) {
        this.state = 'stopped';
        return;
      }
      this.state = 'failed';
      this.lastError =
        `Python 后端意外退出（code=${code} signal=${signal || '-'}）`
        + (this.stderrTail.length
            ? `；错误输出尾部：${this.stderrTail.slice(-3).join(' | ')}`
            : '；无 stderr 输出');
    });

    // ── ③ 轮询等待就绪
    const deadline = Date.now() + START_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (this.state === 'failed') {
        return { ok: false, reused: false, message: this.lastError };
      }
      if (await httpProbe(`${this.baseUrl}/api/health`)) {
        this.state = 'running';
        return { ok: true, reused: false,
                 message: `已启动于 ${this.baseUrl}` };
      }
      await new Promise((r) => setTimeout(r, PROBE_INTERVAL_MS));
    }

    // ★ 超时：必须清理，否则留一个半死不活的后端在后台占端口
    this.state = 'failed';
    this.lastError =
      `启动超时（${START_TIMEOUT_MS / 1000}s 内健康端点无响应）。`
      + (this.stderrTail.length
          ? ` 错误输出：${this.stderrTail.slice(-3).join(' | ')}`
          : ' （无 stderr 输出，通常是首次编译/加载慢，可稍后重试）');
    await this.stop();
    return { ok: false, reused: false, message: this.lastError };
  }

  _tail(s) {
    for (const line of s.split(/\r?\n/)) {
      if (!line.trim()) continue;
      this.stderrTail.push(line.trim());
      if (this.stderrTail.length > 50) this.stderrTail.shift();
    }
  }

  /** 终止后端。★ 必须能重复调用（退出清理会调两次）。 */
  async stop() {
    this.stopping = true;
    const p = this.proc;
    this.proc = null;
    if (!p) {
      if (this.state !== 'failed') this.state = 'stopped';
      this.stopping = false;
      return;
    }
    try {
      if (process.platform === 'win32') {
        // ★ Windows 下 taskkill /T 才会连带杀子进程
        spawn('taskkill', ['/pid', String(p.pid), '/f', '/t'],
              { windowsHide: true });
      } else {
        p.kill('SIGTERM');
      }
    } catch (e) {
      /* 已退出就算了 */
    }
    this.state = 'stopped';
    this.stopping = false;
  }

  /** 健康快照，供「系统状态」页显示。 */
  status() {
    return {
      state: this.state,
      base_url: this.baseUrl,
      pid: this.proc ? this.proc.pid : null,
      python: this.pythonExe,
      root: this.root,
      dev: this.dev,
      last_error: this.lastError,
      stderr_tail: this.stderrTail.slice(-5),
    };
  }
}

module.exports = { BackendManager, httpProbe, portBusy };

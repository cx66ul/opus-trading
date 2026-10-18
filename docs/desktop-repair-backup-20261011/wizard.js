'use strict';
/**
 * Hawk · 首次运行向导
 * ====================================================
 *
 * ★ 为什么要「三态」而不是通过/失败
 * ---------------------------------------------
 * 首次配置里有些项**没配也能用**（比如邮件 SMTP —— 不发信照样能看盘），
 * 有些项**没配就完全不能用**（数据库目录不可写）。
 * 一律当失败 ⇒ 用户被一堆无关项挡住；
 * 一律当通过 ⇒ 真阻断项被跳过，运行时才炸。
 * ⇒ 必须是 `pass` / `skip`（可跳过）/ `block`（阻断）三态，
 *   且每项都带**具体修复动作**（命令或路径），不能只说「失败」。
 *
 * ★ 状态要落盘
 *   重启后不该重复问已经通过的项 —— 重复弹窗会被用户当成坏了。
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

//★★ 状态文件路径必须**惰性求值**。
//   原先在模块顶层写 `app.getPath(...)` ⇒ `require('./wizard')`
//   在 Electron 之外直接抛 `app is not defined`，
//   于是这个模块**根本无法被单独测试**（而它的逻辑恰恰最需要测试：
//   可写性判断、三态划分）。
//   ⇒ 抽成函数，调用时才解析 app；测试时注入假路径。
let _stateFile = null;
function stateFile() {
  if (_stateFile) return _stateFile;
  let dir;
  try {
    dir = require('electron').app.getPath('userData');
  } catch (e) {
    // 不在 Electron 里（单测/脚本）⇒ 退回用户目录
    dir = path.join(os.homedir(), '.hawk');
  }
  return path.join(dir, 'first-run.json');
}
/** 仅供测试注入 */
function _setStateFile(p) { _stateFile = p; }

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(stateFile(), 'utf-8'));
  } catch (e) {
    return { done: false, passed: [], ts: null };
  }
}

function saveState(s) {
  const f = stateFile();
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, JSON.stringify(s, null, 2), 'utf-8');
}

function markDone() {
  const s = loadState();
  s.done = true;
  s.ts = new Date().toISOString();
  saveState(s);
}

/** 可写性检查：写一个临时文件再删掉（比 access 可靠，access 不反映实际可写）。 */
function writable(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    const p = path.join(dir, '.hawk-write-test');
    fs.writeFileSync(p, 'x');
    fs.unlinkSync(p);
    return true;
  } catch (e) {
    return false;
  }
}

/**
 * @returns {Array<{name, level, detail, fix?}>}
 */
function firstRunCheck({ root, python, dev }) {
  const out = [];
  const dataDir = path.join(root, 'data');

  // 1) 数据目录可写 —— 阻断
  const okData = writable(dataDir);
  out.push({
    name: '数据目录可写',
    level: okData ? 'pass' : 'block',
    detail: okData ? dataDir
      : `无法写入 ${dataDir}（通常是权限或磁盘满）`,
    fix: okData ? undefined
      : `以管理员身份运行，或手动创建并授权：\n  mkdir "${dataDir}"`,
  });

  // 2) DuckDB 主库存在 —— 可跳过（首次运行会自动建）
  const db = path.join(dataDir, 'hawk.duckdb');
  const hasDb = fs.existsSync(db);
  out.push({
    name: '行情数据库',
    level: 'pass',
    detail: hasDb ? `${db}（${(fs.statSync(db).size / 1048576).toFixed(1)} MB）`
      : '尚未创建（首次同步后生成，属正常）',
  });

  // 3) Python 解释器 —— 阻断
  const hasPy = fs.existsSync(python);
  out.push({
    name: 'Python 运行时',
    level: hasPy ? 'pass' : 'block',
    detail: hasPy ? python : `未找到：${python}`,
    fix: hasPy ? undefined
      : (dev
        ? '开发模式：安装依赖\n  python -m venv .venv\n  .venv\\Scripts\\pip install -r requirements.txt'
        : '安装包不完整，请重新下载安装（缺 runtime 目录）'),
  });

  // 4) 关键依赖 —— 阻断（缺 duckdb 就完全跑不了）
  let deps = null;
  if (hasPy) {
    try {
      const outp = execFileSync(python,
        ['-c', 'import duckdb,fastapi,uvicorn,httpx;print("ok")'],
        { encoding: 'utf-8', timeout: 20000,
          env: Object.assign({}, process.env, { PYTHONPATH: root }) });
      deps = outp.trim() === 'ok';
    } catch (e) {
      deps = false;
    }
  }
  out.push({
    name: 'Python 依赖',
    level: deps === null ? 'block' : (deps ? 'pass' : 'block'),
    detail: deps === null ? '未检查（Python 缺失）'
      : (deps ? 'duckdb / fastapi / uvicorn / httpx 均可用' : '依赖缺失或版本不符'),
    fix: deps ? undefined
      : `"${python}" -m pip install -r "${path.join(root, 'requirements.txt')}"`,
  });

  // 5) SQLite 侧库（语料/自选/持仓）—— 可跳过
  const lite = path.join(dataDir, 'hawk.sqlite');
  out.push({
    name: '辅助库（自选/持仓/语料）',
    level: 'pass',
    detail: fs.existsSync(lite) ? lite : '尚未创建（首次使用时生成）',
  });

  // 6) 邮件配置 —— **可跳过**：不发信不影响看盘
  const secrets = path.join(root, 'data', 'secrets.json');
  out.push({
    name: '邮件通知',
    level: fs.existsSync(secrets) ? 'pass' : 'skip',
    detail: fs.existsSync(secrets) ? '已配置'
      : '未配置（不影响看盘与推荐，仅不能发盘前/午间报告）',
    fix: fs.existsSync(secrets) ? undefined
      : 'python scripts/setup_secrets.py',
  });

  // 7) 行情数据新鲜度 —— 可跳过但要提示
  const staleHint = '未知（需连接数据库读取）';
  out.push({
    name: '行情数据',
    level: 'skip',
    detail: staleHint,
    fix: 'python scripts/sync.py --daily',
  });

  return out;
}

module.exports = { firstRunCheck, loadState, saveState, markDone,
                 stateFile, _setStateFile };

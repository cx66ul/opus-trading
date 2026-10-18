'use strict';
/**
 * Opus 辅助交易 · Electron 主进程
 * ====================================================
 *
 * ★ 三条桌面端特有的纪律
 * ---------------------------------------------
 * 1. **单实例锁**：第二次启动要把窗口**唤醒**而不是再开一个窗口 ——
 *    两个实例会抢同一个 DuckDB 写锁，表现为「数据库已被占用」，
 *    而用户完全不知道是自己开了两次。
 * 2. **关窗 ≠ 退出**：关窗最小化到托盘（行情类应用应有行为）；
 *    只有「退出」菜单项与系统退出才真正停后端。
 *    ★ 若关窗即退，用户每看一次盘都要重开一次服务（十几秒），
 *      久而久之就改成不用了 —— 这是桌面端最常见的「做了没人用」。
 * 3. **退出必须清场**：停后端、清临时文件。
 *    留孤儿进程占端口 ⇒ 下次启动报「端口被占用」。
 *
 * 开发模式：electron . （连本机的 uvicorn）
 * 生产模式：打包后自带 runtime
 */

const { app, BrowserWindow, Tray, Menu, dialog, ipcMain, shell, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { BackendManager } = require('./backend');
const { firstRunCheck, markDone, loadState } = require('./wizard');
const { Updater } = require('./updater');

const IS_DEV = !app.isPackaged;
// dev：项目根在上一级；prod：解包后的 resources/app 旁边
const ROOT = IS_DEV
  ? path.resolve(__dirname, '..')
  : path.join(process.resourcesPath, 'app');
const PYTHON = IS_DEV
  ? (process.env.HAWK_PYTHON || path.join(ROOT, '.venv', 'Scripts', 'python.exe'))
  : path.join(process.resourcesPath, 'runtime', 'python.exe');
const PORT = parseInt(process.env.HAWK_PORT || '8870', 10);

// ★ 单实例：必须在 ready 之前拿锁
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

let mainWindow = null;
let tray = null;
let backend = null;
let quitting = false;

// ────────────────────────────────────────────── 窗口
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1100,
    minHeight: 700,
    show: false,                     // ★ 等后端就绪再 show，避免白屏
    backgroundColor: '#0f1115',
    // ★ 图标：dev 用 assets/，打包后 resources 在别处 ⇒ 两个路径都要试，
    //   找不到就**不设**（Electron 会回退默认图标），
    //   绝不能因为找不到路径就抛异常导致窗口都开不出来。
    icon: pickIcon(),
    title: 'Opus 辅助交易',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,        // ★ 渲染进程不给 Node 权限
      sandbox: true,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());

  mainWindow.on('close', (e) => {
    // ★ 关窗 ⇒ 最小化到托盘，而非退出（见文件头纪律 2）
    if (!quitting && tray) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => { mainWindow = null; });

  // ★ 外部链接交给系统浏览器，别在应用内开新窗口
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  return mainWindow;
}

function loadApp() {
  // ★ 第一屏加载**壳层**（file://），后端就绪后由壳层跳到 http:// 主界面。
  //   反过来（直接 loadURL 主界面）的话，后端没起来时只剩一个浏览器错误页，
  //   用户既看不到原因也没有重试入口 —— 而这正是最需要引导的时刻。
  mainWindow.loadFile(path.join(__dirname, 'shell.html'));
}

// ────────────────────────────────────────────── 托盘
/** 按「打包后 / 开发中」两种布局找图标文件。 */
function pickIcon(name) {
  const candidates = [
    path.join(__dirname, 'assets', name || 'icon.png'),
    path.join(process.resourcesPath || '', 'assets', name || 'icon.png'),
    path.join(__dirname, '..', 'desktop', 'assets', name || 'icon.png'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      const img = nativeImage.createFromPath(p);
      if (!img.isEmpty()) return p;
    }
  }
  return undefined;
}

function createTray() {
  // ★ 托盘用 32px 版本；16px 在高分屏上会糊成一团
  const trayPath = pickIcon('tray.png');
  let icon = trayPath ? nativeImage.createFromPath(trayPath) : nativeImage.createEmpty();
  if (icon.isEmpty()) {
    // 兜底：退回主图标，别显示成空白托盘
    const alt = pickIcon('icon.png');
    if (alt) icon = nativeImage.createFromPath(alt);
  }

  tray = new Tray(icon);
  tray.setToolTip('Opus 辅助交易');
  refreshTrayMenu();
  tray.on('click', () => {
    if (mainWindow.isVisible()) mainWindow.hide();
    else { mainWindow.show(); mainWindow.focus(); }
  });
}

function refreshTrayMenu() {
  if (!tray) return;
  const st = backend ? backend.status() : { state: 'idle' };
  const label = {
    idle: '未启动', starting: '启动中…', running: '运行中',
    stopped: '已停止', failed: '异常',
  }[st.state] || st.state;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `后端：${label}`, enabled: false },
    { type: 'separator' },
    { label: '显示主窗口', click: () => { mainWindow.show(); mainWindow.focus(); } },
    { label: '重新启动后端', click: async () => { await backend.stop(); await backend.start(); refreshTrayMenu(); } },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit(); } },
  ]));
}

// ────────────────────────────────────────────── 启动流程
async function boot() {
  createWindow();

  // ★ 先跑首次向导的**只读检查**，把结论显示出来；
  //   有阻断项时让用户看到，而不是先转 30 秒圈再报白屏。
  const checks = firstRunCheck({ root: ROOT, python: PYTHON, dev: IS_DEV });
  backend = new BackendManager({
    port: PORT, root: ROOT, pythonExe: PYTHON, dev: IS_DEV,
  });

  //★★★ 时序：**先加载壳层，再起后端**。
  //   原实现把 `backend.start()` 挂在 `did-finish-load` 上，
  //   而 `did-finish-load` 只有**加载了页面之后**才触发
  //   ⇒ 我又没在 before-quit 之前调 loadApp()
  //   ⇒ 互相等待，窗口永远空白、后端永远不启动。
  //   实测症状：CDP 里 page 的 url 与 title 都是空的，
  //   而日志里**一行错误都没有**（最难查的一类）。
  //   ⇒ 正确顺序：loadApp() → 等 did-finish-load → 起后端 → 推状态。
  mainWindow.webContents.on('did-finish-load', async () => {
    const r = await backend.start();
    refreshTrayMenu();
    mainWindow.webContents.send('boot-status', {
      backend: backend.status(), checks, first_run: !loadState().done,
    });
    if (!r.ok) {
      // ★ 不弹系统对话框：壳层已有专门的错误屏（含重试与诊断复制），
      //   再弹一个 modal 会挡住用户点「重试」。
      // ★ `console.error` 而不是 `logger &&`—— 后者在本文件里
      //   没有 logger 变量，未声明标识符即使写 `x && y` 也会抛
      //   ReferenceError，把错误信息本身给吞掉。
      console.error(`[backend] 启动失败: ${r.message}`);
    }
  });

  // ★ 兜底：壳层没加载出来时（打包漏文件等）给明确提示
  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url) => {
    console.error(`[shell] 加载失败 code=${code} desc=${desc} url=${url}`);
  });

  loadApp();
}

app.whenReady().then(() => {
  createTray();
  boot();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else { mainWindow.show(); mainWindow.focus(); }
  });
});

// ────────────────────────────────────────────── IPC（渲染进程能做的事，仅限这些）
ipcMain.handle('backend:status', () => (backend ? backend.status() : { state: 'idle' }));
ipcMain.handle('backend:restart', async () => {
  if (!backend) return { ok: false, message: '未初始化' };
  await backend.stop();
  const r = await backend.start();
  refreshTrayMenu();
  return r;
});
ipcMain.handle('wizard:state', () => loadState());
ipcMain.handle('wizard:finish', () => { markDone(); return { ok: true }; });
ipcMain.handle('app:info', () => ({
  version: app.getVersion(),
  root: ROOT, python: PYTHON, port: PORT, dev: IS_DEV,
  data_dir: app.getPath('userData'),
}));
ipcMain.handle('update:check', async () => {
  const u = new Updater({ root: ROOT, version: app.getVersion() });
  return u.check();
});

// ────────────────────────────────────────────── 退出清场
app.on('window-all-closed', () => {
  // 托盘常驻 ⇒ 不退出（Windows/Linux 习惯）
  if (process.platform !== 'darwin' && !tray) app.quit();
});

app.on('before-quit', async (e) => {
  if (quitting || !backend) return;
  // ★ 拦一次，等后端真正停掉再退 —— 否则会留孤儿占端口
  e.preventDefault();
  quitting = true;
  await backend.stop();
  app.quit();
});

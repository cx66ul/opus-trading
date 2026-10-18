'use strict';
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const { BackendManager } = require('./backend');
const { firstRunCheck, markDone, loadState } = require('./wizard');
const { Updater } = require('./updater');

const IS_DEV = !app.isPackaged;
// Local deployments may explicitly reuse the original project and its databases.
let localSettings = {};
try { localSettings = JSON.parse(fs.readFileSync(path.join(path.dirname(app.getPath('exe')), 'local-settings.json'), 'utf8')); } catch {}
const ROOT = process.env.HAWK_ROOT || localSettings.root || (IS_DEV
  ? path.resolve(__dirname, '..') : path.join(process.resourcesPath, 'app'));
const runtimeDir = path.join(process.resourcesPath, 'runtime');
const PYTHON = process.env.HAWK_PYTHON || (IS_DEV
  ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
  : [path.join(runtimeDir, 'python.exe'), path.join(runtimeDir, 'Scripts', 'python.exe')]
      .find(p => fs.existsSync(p)) || path.join(runtimeDir, 'python.exe'));
const PORT = Number(process.env.HAWK_PORT || 8870);
let mainWindow = null;
let tray = null;
let backend = null;
let quitting = false;
let cleaned = false;
let checks = [];
let startup = null;
let navigationError = '';
const startedAt = Date.now();
function log(event, detail = '') {
  const line = JSON.stringify({ at: new Date().toISOString(), ms: Date.now() - startedAt, event, detail }) + '\n';
  try {
    const dir = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, 'startup.log'), line);
  } catch {}
  console.log(line.trim());
}
function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) app.quit();
else app.on('second-instance', () => { log('second-instance'); showWindow(); });

function pickIcon(name) {
  const candidates = [
    path.join(__dirname, 'assets', name || 'icon.png'),
    path.join(process.resourcesPath || '', 'assets', name || 'icon.png'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p) && !nativeImage.createFromPath(p).isEmpty()) return p;
  }
  return undefined;
}
function snapshot() {
  const status = backend ? backend.status() : { state: 'idle' };
  return { ...status, checks, navigation_error: navigationError };
}
function sendStatus() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('boot-status', { backend: snapshot(), checks, first_run: !loadState().done });
  }
  refreshTrayMenu();
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
async function emergencyPage(message) {
  log('display-error', message);
  if (!mainWindow || mainWindow.isDestroyed()) return;
  // Inline, independent of packaged shell files: even a broken package has a visible explanation.
  await mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
    '<html lang="zh-CN"><meta charset="utf-8"><title>Opus 启动诊断</title>' +
    '<body style="background:#0f1115;color:#e8eaed;font:16px Microsoft YaHei;padding:60px">' +
    '<h1>Opus 辅助交易未能加载界面</h1><p>' + escapeHtml(message) +
    '</p><p>请重新安装完整版本。诊断日志：' +
    escapeHtml(path.join(app.getPath('userData'), 'logs', 'startup.log')) + '</p></body></html>'));
  showWindow();
}
async function loadApp() {
  try { await mainWindow.loadFile(path.join(__dirname, 'shell.html')); }
  catch (e) { await emergencyPage(e.message); }
}
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440, height: 940, minWidth: 1100, minHeight: 700,
    show: false, backgroundColor: '#0f1115', icon: pickIcon(),
    title: 'Opus 辅助交易', autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.once('ready-to-show', () => { showWindow(); log('window-visible'); });
  mainWindow.on('close', e => {
    if (!quitting && tray) { e.preventDefault(); mainWindow.hide(); }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (e, url) => {
    const allowed = url.startsWith('file:') || (backend && url.startsWith(backend.baseUrl + '/'));
    if (!allowed) e.preventDefault();
  });
  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    log('load-failed', { code, desc, url });
    if (url.startsWith('http:')) {
      navigationError = '主界面加载失败：' + desc + '。请重试本地服务。';
      void loadApp();
    }
  });
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    log('renderer-gone', details);
    navigationError = '界面进程已退出，请点击重试恢复。';
    void loadApp();
  });
  mainWindow.webContents.on('unresponsive', () => log('renderer-unresponsive'));
  return mainWindow;
}
function createTray() {
  const p = pickIcon('tray.png') || pickIcon();
  tray = new Tray(p ? nativeImage.createFromPath(p) : nativeImage.createEmpty());
  tray.setToolTip('Opus 辅助交易');
  tray.on('click', showWindow);
  refreshTrayMenu();
}
function refreshTrayMenu() {
  if (!tray) return;
  const st = backend ? backend.state : 'idle';
  const label = { idle: '未启动', starting: '启动中…', running: '运行中', stopped: '已停止', failed: '异常' }[st] || st;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '后端：' + label, enabled: false },
    { type: 'separator' },
    { label: '显示主窗口', click: showWindow },
    { label: '重新启动后端', click: () => { void restartBackend(); } },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ]));
}
async function startBackend() {
  navigationError = '';
  checks = await firstRunCheck({ root: ROOT, python: PYTHON, dev: IS_DEV, probeDependencies: false });
  sendStatus();
  const result = await backend.start();
  log('backend-ready', { ok: result.ok, message: result.message });
  if (!result.ok) console.error('[backend] ' + result.message);
  sendStatus();
  return result;
}
async function restartBackend() {
  if (startup) await startup;
  startup = (async () => {
    await backend.stop();
    await loadApp();
    return startBackend();
  })();
  try { return await startup; } finally { startup = null; }
}
async function boot() {
  backend = new BackendManager({ port: PORT, root: ROOT, pythonExe: PYTHON, dev: IS_DEV });
  backend.on('status', () => {
    sendStatus();
    if (backend.state === 'failed' && mainWindow &&
        mainWindow.webContents.getURL().startsWith(backend.baseUrl + '/')) {
      void loadApp();
    }
  });
  createWindow();
  createTray();
  // Only the first shell load starts the service. Main-page reloads never restart Python.
  mainWindow.webContents.on('did-finish-load', () => {
    log('page-loaded', mainWindow.webContents.getURL());
    sendStatus();
  });
  await loadApp();
  log('shell-loaded');
  startup = startBackend();
  try { await startup; } finally { startup = null; }
}
if (gotLock) {
  app.whenReady().then(() => boot()).catch(e => {
    console.error(e);
    void emergencyPage(e.message);
  });
}
app.on('activate', () => { if (mainWindow) showWindow(); });
ipcMain.handle('backend:status', () => snapshot());
ipcMain.handle('backend:restart', () => restartBackend());
ipcMain.handle('wizard:state', () => loadState());
ipcMain.handle('wizard:finish', () => {
  if (checks.some(c => c.level === 'block') || backend.state !== 'running') return { ok: false };
  markDone();
  return { ok: true };
});
ipcMain.handle('app:info', () => ({
  version: app.getVersion(), root: ROOT, python: PYTHON, port: PORT, dev: IS_DEV,
  data_dir: path.join(ROOT, 'data'), log_dir: path.join(app.getPath('userData'), 'logs'),
}));
ipcMain.handle('update:check', () => new Updater({ root: ROOT, version: app.getVersion() }).check());
app.on('window-all-closed', () => { if (!tray) app.quit(); });
app.on('before-quit', e => {
  if (cleaned || !backend) return;
  e.preventDefault();
  if (quitting) return;
  quitting = true;
  void (async () => {
    if (startup) await startup.catch(() => {});
    await backend.stop();
    log('backend-stopped');
    cleaned = true;
    app.quit();
  })().catch(e => { log('shutdown-failed', e.message); quitting = false; });
});

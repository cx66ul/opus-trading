'use strict';
const $ = s => document.querySelector(s);
const bridge = (typeof window !== 'undefined' && window.hawk) ? window.hawk : null;
let poll = null;
let navigating = false;
let inspecting = false;
let retrying = false;
function show(id) {
  document.querySelectorAll('.screen').forEach(e => e.classList.remove('on'));
  const el = document.getElementById(id);
  if (el) el.classList.add('on');
}
function showError(msg, detail) {
  $('#err-msg').textContent = msg || '未知原因';
  $('#err-detail').textContent = detail || '';
  show('scr-error');
}
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function renderChecks(checks) {
  const levels = { pass: '通过', skip: '可跳过', block: '阻断' };
  $('#wiz-body').innerHTML = (checks || []).map(c =>
    '<tr><td>' + esc(c.name) + '</td><td><span class="tag ' + esc(c.level) + '">' +
    levels[c.level] + '</span></td><td>' + esc(c.detail) + '</td><td>' +
    (c.fix ? '<code>' + esc(c.fix) + '</code>' : '—') + '</td></tr>').join('');
  $('#btn-wiz-continue').disabled = (checks || []).some(c => c.level === 'block');
}
async function applyStatus(be) {
  if (!be || navigating || retrying) return;
  const checks = be.checks || [];
  const blocked = checks.some(c => c.level === 'block');
  if (be.navigation_error) {
    showError(be.navigation_error, JSON.stringify(be, null, 2));
  } else if (be.state === 'failed' || be.state === 'stopped') {
    showError(be.last_error || '本地服务已停止，请点击重试。', JSON.stringify(be, null, 2));
  } else if (blocked || inspecting) {
    renderChecks(checks);
    $('#btn-wiz-continue').disabled = blocked || be.state !== 'running';
    show('scr-wizard');
  } else if (be.state === 'running') {
    navigating = true;
    clearInterval(poll);
    await bridge.finishWizard().catch(() => {});
    window.location.replace(be.base_url + '/');
  } else {
    show('scr-boot');
  }
}
async function refresh() {
  try { await applyStatus(await bridge.backendStatus()); }
  catch (e) { showError('无法连接桌面服务，请重新打开应用。', String(e)); }
}
async function boot() {
  if (!bridge) {
    showError('桌面桥接未加载（preload 不可用）。', 'window.hawk is undefined\n请重新安装完整版本。');
    return;
  }
  // Subscribe before any IPC awaits; the initial status can arrive immediately.
  bridge.onBoot(payload => { void applyStatus(payload.backend || {}); });
  poll = setInterval(() => { void refresh(); }, 400);
  await refresh();
}
async function retry() {
  inspecting = false;
  retrying = true;
  $('#btn-retry').disabled = true;
  show('scr-boot');
  try {
    const r = await bridge.restartBackend();
    if (!r.ok) showError(r.message, JSON.stringify(r, null, 2));
  } catch (e) { showError('重新启动失败', String(e)); }
  finally { retrying = false; $('#btn-retry').disabled = false; await refresh(); }
}
document.addEventListener('DOMContentLoaded', () => {
  $('#btn-retry').addEventListener('click', async () => {
    if (!bridge) return;
    await retry();
  });
  $('#btn-copy').addEventListener('click', async () => {
    if (!bridge) return;
    const info = await bridge.appInfo().catch(() => ({}));
    const be = await bridge.backendStatus().catch(() => ({}));
    const text = JSON.stringify({ info, backend: be }, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      $('#btn-copy').textContent = '已复制';
    } catch { $('#err-detail').textContent = text; }
  });
  $('#btn-wizard').addEventListener('click', async () => {
    if (!bridge) return;
    inspecting = true;
    renderChecks((await bridge.backendStatus()).checks);
    show('scr-wizard');
  });
  $('#btn-wiz-recheck').addEventListener('click', async () => {
    if (!bridge) return;
    await retry();
  });
  $('#btn-wiz-continue').addEventListener('click', async () => {
    if (!bridge) return;
    inspecting = false;
    await refresh();
  });
  void boot();
});

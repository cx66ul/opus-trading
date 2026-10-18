/* Opus 辅助交易 · 桌面壳层逻辑
 *
 * ★ 为什么不写成原生 JS 就完事，还要这么小心
 * --------------------------------------------------
 * 壳层的失败模式比主界面多一层：**主进程没起来时它也得能显示**。
 * 所以这里不能假设 `window.hawk` 一定存在 ——
 * preload 加载失败 / 主进程异常时它是 undefined，
 * 此时页面必须**降级到「显示错误」而不是白屏**。
 */
'use strict';

const $ = (s) => document.querySelector(s);

function show(id) {
  document.querySelectorAll('.screen').forEach((e) => e.classList.remove('on'));
  const el = document.getElementById(id);
  if (el) el.classList.add('on');
}

/** ★ 主进程桥接可能不存在（preload 失败 / 打包配置错）。
 *  没有它就不能继续，但也不能白屏 —— 要说清是哪儿坏了。 */
const bridge = (typeof window !== 'undefined' && window.hawk) ? window.hawk : null;

function showError(msg, detail) {
  $('#err-msg').textContent = msg || '未知原因';
  $('#err-detail').textContent = detail || '';
  show('scr-error');
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ────────────────────────────────────── 向导渲染
function renderChecks(checks) {
  const body = $('#wiz-body');
  const LEVELS = { pass: '通过', skip: '可跳过', block: '阻断' };
  body.innerHTML = (checks || []).map((c) => {
    // ★ 修复动作**原样展示**，不转成一句「请联系管理员」——
    //   向导的价值就在于给出可复制的命令。
    const fix = c.fix
      ? `<code>${esc(c.fix)}</code>`
      : '<span class="dim">—</span>';
    return `<tr>
      <td class="l">${esc(c.name)}</td>
      <td class="w"><span class="tag ${esc(c.level)}">${LEVELS[c.level] || c.level}</span></td>
      <td class="l">${esc(c.detail)}</td>
      <td class="l">${fix}</td>
    </tr>`;
  }).join('');
  // 有阻断项时禁用「进入应用」—— 用了会出问题，不如明确拦
  const blocked = (checks || []).some((c) => c.level === 'block');
  const btn = $('#btn-wiz-continue');
  btn.disabled = blocked;
  btn.title = blocked ? '有阻断项，必须先修复' : '';
}

// ────────────────────────────────────── 启动流程
async function boot() {
  if (!bridge) {
    // 最外层的失败：连桥都没有 ⇒ 只能把诊断信息摆出来
    showError(
      '桌面桥接未加载（preload 不可用）。',
      'window.hawk is undefined\n'
      + '常见原因：\n'
      + '  1) 打包时 preload.js 没进 files\n'
      + '  2) contextIsolation 配置与 preload 不匹配\n'
      + '  3) 主进程启动即崩溃');
    return;
  }

  // 首次运行 ⇒ 先跑向导
  let st = { done: false };
  try { st = await bridge.wizardState(); } catch (e) { /* 当作首次 */ }

  const info = await bridge.appInfo().catch(() => ({}));
  $('#boot-msg').textContent = `正在启动本地服务（端口 ${info.port || '—'}）…`;

  // 等主进程推送 boot-status
  window.__onBoot = (payload) => {
    const be = payload.backend || {};
    if (payload.checks && (!st || !st.done)) {
      renderChecks(payload.checks);
      show('scr-wizard');
      return;
    }
    if (be.state === 'running') {
      //★ 后端就绪 ⇒ 交回主进程切到主界面
      window.location.replace(be.base_url + '/');
      return;
    }
    showError(be.last_error || '本地服务未能启动',
      JSON.stringify(be, null, 2));
  };
  bridge.onBoot(window.__onBoot);

  // 兜底轮询：万一 boot-status 在注册前就发过了
  const poll = setInterval(async () => {
    const be = await bridge.backendStatus().catch(() => null);
    if (!be) return;
    if (be.state === 'running') {
      clearInterval(poll);
      window.location.replace(be.base_url + '/');
    } else if (be.state === 'failed') {
      clearInterval(poll);
      showError(be.last_error, JSON.stringify(be, null, 2));
    }
  }, 1200);
}

// ────────────────────────────────────── 事件绑定
document.addEventListener('DOMContentLoaded', () => {
  //★ 所有按钮回调都要在 bridge 为 null 时也能安全响应
  //  （shellError 路径下 bridge 就是 undefined，
  //   点击「重试」会抛 TypeError 覆盖掉原本的错误信息）。
  $('#btn-retry').addEventListener('click', async () => {
    if (!bridge) return;
    $('#btn-retry').disabled = true;
    const r = await bridge.restartBackend().catch((e) => ({ ok: false, message: String(e) }));
    $('#btn-retry').disabled = false;
    if (r.ok) window.location.reload();
    else showError(r.message, JSON.stringify(r, null, 2));
  });

  $('#btn-copy').addEventListener('click', async () => {
    if (!bridge) return;
    const be = await bridge.backendStatus().catch(() => ({}));
    const info = await bridge.appInfo().catch(() => ({}));
    const text = [
      `时间: ${new Date().toISOString()}`,
      `版本: ${info.version || '-'}  dev=${info.dev}`,
      `Python: ${info.python || '-'}`,
      `项目根: ${info.root || '-'}`,
      `端口: ${info.port || '-'}`,
      `状态: ${be.state || '-'}`,
      `错误: ${be.last_error || '-'}`,
      `stderr 尾部:\n${(be.stderr_tail || []).join('\n')}`,
    ].join('\n');
    try {
      await navigator.clipboard.writeText(text);
      $('#btn-copy').textContent = '已复制';
      setTimeout(() => { $('#btn-copy').textContent = '复制诊断信息'; }, 1500);
    } catch (e) {
      $('#err-detail').textContent = text;   // ★ 剪贴板不可用时至少能手动选
    }
  });

  $('#btn-wizard').addEventListener('click', async () => {
    if (!bridge) return;
    // ★ 重跑检查：让用户改完环境后能立刻复检，
    //   而不是「关掉重开应用」—— 那会让人以为必须重启才行。
    const be = await bridge.backendStatus().catch(() => null);
    if (be && be.checks) renderChecks(be.checks);
    show('scr-wizard');
  });

  // ★ 同样加守卫：bridge 为 null 时说明壳层根本没被主进程接管，
  //   这时 reload 只会把用户弹回同一个错误页（无限循环），
  //   不如就地停下并把情况说清楚。
  $('#btn-wiz-recheck').addEventListener('click', async () => {
    if (!bridge) {
      showError('桌面桥接未加载，无法复检。',
        '请重启应用；若仍如此，说明 preload.js 未随包分发。');
      return;
    }
    window.location.reload();
  });

  $('#btn-wiz-continue').addEventListener('click', async () => {
    if (!bridge) return;
    await bridge.finishWizard().catch(() => {});
    window.location.reload();
  });

  boot();
});

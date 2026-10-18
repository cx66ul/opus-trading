/* ★ Opus 辅助交易前端
 *
 * 三条纪律：
 *  1. **不复制计算逻辑**：涨跌家数/板块强度/缠论信号全部由后端算好，
 *     前端只做展示与格式化。⇒ 不会出现「网页与邮件结论不一致」。
 *  2. **配色铁律**：涨=红、跌=绿。颜色统一从 /api/overview 的 colors 字段取，
 *     **不在前端硬编码**，避免后端改了口径前端还留着旧配色。
 *  3. **残缺日显式拦截**：data_usable=false 时页面顶部挂警告条，
 *     且不展示涨跌家数（后端已返回 null）。
 */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  day: null,          // 当前交易日（后端归一后的结果，★不用浏览器时间）
  usable: null,
  colors: { up: '#e0392e', down: '#1a9e5f', flat: '#8a8f99' },
  stock: null,        // 当前个股详情缓存
};

// ────────────────────────────────────────────── 工具
function cls(v) {
  if (v == null) return 'flat';
  return v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
}
function pct(v, d = 2) {
  if (v == null) return '—';
  return (v > 0 ? '+' : '') + (v * 100).toFixed(d) + '%';
}
function num(v, d = 2) {
  if (v == null) return '—';
  return Number(v).toFixed(d);
}
function yi(v) {
  if (v == null) return '—';
  return (v / 10000).toFixed(2) + ' 万亿';
}
/** 把「股」量级缩写成万手/亿股。
 *
 * ★ 单位必须显式标注：库里 volume 是**股**（不是手），
 *   而股票软件习惯显示「手」。⇒ 1 手 = 100 股，换算写在这里只有一处。
 */
function fmtVol(v) {
  if (v == null) return '—';
  const hands = v / 100;
  if (hands >= 1e8) return (hands / 1e8).toFixed(2) + ' 亿手';
  if (hands >= 1e4) return (hands / 1e4).toFixed(2) + ' 万手';
  return Math.round(hands) + ' 手';
}
function err(id, e) {
  const el = $('#' + id);
  if (!el) return;
  const msg = (e && e.detail) ? e.detail : (e && e.message) || String(e);
  el.innerHTML = '<div class="err">' + esc(msg) + '</div>';
}
function clearErr(id) { const el = $('#' + id); if (el) el.innerHTML = ''; }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ detail: '响应不是 JSON' }));
  if (!r.ok) { const e = new Error(j.detail || r.statusText); e.detail = j.detail; throw e; }
  return j;
}

// ────────────────────────────────────────────── 页面切换
const PAGES = {
  overview: loadOverview,
  watch: loadWatch,
  recommend: loadRecommend,
  stock: () => { if (!S.stock) queryStock(); },
  boards: loadBoards,
  health: loadHealth,
};
$$('nav button').forEach((b) => {
  b.onclick = () => {
    $$('nav button').forEach((x) => x.classList.remove('on'));
    $$('section.page').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    $('#p-' + b.dataset.p).classList.add('on');
    PAGES[b.dataset.p]();
  };
});

function setDay(d, usable) {
  S.day = d; S.usable = usable;
  $('#hdr-day').textContent = '交易日 ' + d;
  const u = $('#hdr-usable');
  if (usable === false) {
    u.style.display = ''; u.className = 'tag warn';
    u.textContent = '残缺日：不可做横截面判断';
  } else { u.style.display = 'none'; }
}
// ────────────────────────────────────────────── 概览
async function loadOverview() {
  clearErr('ov-err');
  try {
    const q = S.day ? '?as_of=' + S.day : '';
    const d = await api('/api/overview' + q);
    setDay(d.trade_date, d.data_usable);
    if (d.colors) S.colors = d.colors;

    const m = d.market || {};
    const dr = d.day_resolution || {};
    // ★ 这两条是**口径提示**不是错误 ⇒ 用独立的 .warn 类，不复用 .err。
//   理由：`.err` 是「操作失败」的语义（红框），而回退提示是「系统已按规则
//   自动降级、结果可用」（黄框）。混用会让「页面上有错误」这个判据失去意义
//   —— 实测验收器扫 `.err` 时把回退提示当成了故障。
const warn = dr.fallback
      ? `<div class="warn">行情表最新是 <b>${esc(dr.requested)}</b>，但该日残缺
         ⇒ 已自动回退到最近<b>可用</b>交易日 <b>${esc(dr.used)}</b>
         （库内共 ${dr.usable_days} 个可用日）。</div>`
      : (d.data_usable ? '' :
        `<div class="warn">${esc(d.trade_date)} 是残缺日（标的数不足 ${d.universe_threshold}），
         下方涨跌家数与成交额<b>不可用于横截面判断</b>，已按 data_gate 拦截。</div>`);

    $('#ov-kpi').innerHTML = `
      ${warn}
      <div class="kpi"><div class="lb">全市场成交额（volume×close）</div>
        <div class="vl">${d.data_usable ? yi(d.total_amount_yi) : '—'}</div>
        <div class="ex">${d.n_stock} 只有K 线</div></div>
      <div class="kpi"><div class="lb">涨 / 跌家数</div>
        <div class="vl"><span class="up">${d.up == null ? '—' : d.up}</span> /
             <span class="down">${d.down == null ? '—' : d.down}</span></div>
        <div class="ex">分母 ${d.breadth_base == null ? '—' : d.breadth_base}（有昨收的样本）</div></div>
      <div class="kpi"><div class="lb">市场状态</div>
        <div class="vl">${esc(m.state_label || m.state || '—')}</div>
        <div class="ex">score ${num(m.score, 1)} · 仓位上限 ${pct(m.cap_hint ?? m.position_cap, 0)}</div></div>
      <div class="kpi"><div class="lb">涨停 / 跌停家数</div>
        <div class="vl"><span class="up">${m.limit_up_cnt == null ? '—' : m.limit_up_cnt}</span> /
             <span class="down">${m.limit_down_cnt == null ? '—' : m.limit_down_cnt}</div>
        <div class="ex">炸板率 ${pct(m.broken_rate)}</div></div>`;

    $('#ov-mkt').innerHTML = `
      <h2>市场状态明细 <small>market_regime</small></h2>
      ${m.ts ? `<div class="kv">
        <div class="k">记录时间</div><div>${esc(m.ts)}</div>
        <div class="k">状态</div><div>${esc(m.state)}（${esc(m.state_label || '—')}）</div>
        <div class="k">评分</div><div>${num(m.score, 2)}</div>
        <div class="k">上涨占比</div><div>${pct(m.up_ratio)}</div>
        <div class="k">炸板率</div><div>${pct(m.broken_rate)}</div>
      </div>` : '<div class="note">market_regime 表为空，先跑 sync_regime。</div>'}
      ${m.stale_days ? `<div class="note warn">${esc(m.note || '')}</div>` : ''}`;

    $('#ov-notes').innerHTML = `
      · as_of：${esc(d.ts_note)}（当前 ${esc(d.trade_date)}）<br>
      · 成交额由 <code>volume × close</code> 现算——<code>kline_daily.amount</code> 全库为 NULL，
        <code>kline_min.amount</code> 已废弃（逐股漂移实测跨 4 个数量级）。<br>
      · 涨跌家数的分母是「<b>有上一根 K 线收盘</b>」的样本数，不是当日标的数；
        新股/停牌不参与。<br>
      · 配色：<b style="color:${S.colors.up}">涨=红</b>、
        <b style="color:${S.colors.down}">跌=绿</b>，全局不得反转。`;
  } catch (e) { err('ov-err', e); }
}

// ────────────────────────────────────────────── 自选股
async function loadWatch() {
  loadTrackedNews();
  clearErr('w-search');
  $('#w-body').innerHTML = '<div class="note">加载中…</div>';
  try {
    const q = S.day ? '?as_of=' + S.day : '';
    const d = await api('/api/watchlist' + q);
    $('#w-day').textContent = d.trade_date;
    if (!d.rows.length) {
      $('#w-body').innerHTML =
        `<div class="note">${esc(d.note || '自选股为空')}　当前库内
         <code>watchlist</code> 为 0 行 —— 这是**真实状态**（M6 尚未录入），
         不是加载失败。可在上方搜索添加。</div>`;
      return;
    }
    $('#w-body').innerHTML = `<table><thead><tr>
      <th class="l">代码</th><th class="l">名称</th><th class="l">分组</th>
      <th>收盘</th><th>涨跌</th><th>成交量(股)</th><th class="l">加入理由</th><th></th>
      </tr></thead><tbody>${d.rows.map((r) => `<tr data-c="${esc(r.code)}">
        <td class="l">${esc(r.code)}</td><td class="l">${esc(r.name)}</td>
        <td class="l"><span class="tag">${esc(r.group)}</span></td>
        <td class="num">${num(r.close)}</td>
        <td class="num ${cls(r.pct_chg)}">${pct(r.pct_chg)}</td>
        <td class="num">${r.volume == null ? '—' : Math.round(r.volume).toLocaleString()}</td>
        <td class="l">${esc(r.reason || '')}</td>
        <td class="l"><button class="act" data-del="${esc(r.code)}">删除</button></td>
      </tr>`).join('')}</tbody></table>`;
    $('#w-body').querySelectorAll('tr[data-c]').forEach((tr) => {
      tr.onclick = (ev) => {
        if (ev.target.dataset.del) return;
        $('#s-q').value = tr.dataset.c;
        $$('nav button').forEach((x) => x.classList.toggle('on', x.dataset.p === 'stock'));
        $$('section.page').forEach((x) => x.classList.toggle('on', x.id === 'p-stock'));
        queryStock();
      };
    });
    $('#w-body').querySelectorAll('button[data-del]').forEach((b) => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm('从自选移除 ' + b.dataset.del + '？')) return;
        try {
          await api('/api/watchlist/' + b.dataset.del + '?as_of=' + S.day, { method: 'DELETE' });
          loadWatch();
        } catch (e) { err('w-search', e); }
      };
    });
  } catch (e) { $('#w-body').innerHTML = ''; err('w-body', e); }
}

async function doSearch(targetId) {
  const q = $('#w-q').value.trim();
  if (!q) return;
  try {
    const d = await api('/api/search?q=' + encodeURIComponent(q));
    const box = $('#' + targetId);
    if (!d.rows.length) { box.innerHTML = '<div class="note">无匹配</div>'; return; }
    box.innerHTML = '匹配：' + d.rows.map((r) =>
      `<button class="act" data-c="${esc(r.code)}" data-n="${esc(r.name)}">${esc(r.code)} ${esc(r.name)}</button>`
    ).join(' ');
    box.querySelectorAll('button[data-c]').forEach((b) => {
      b.onclick = () => $('#w-q').value = b.dataset.c + ' ' + (b.dataset.n || '');
    });
  } catch (e) { err('w-search', e); }
}

$('#w-add').onclick = async () => {
  const q = $('#w-q').value.trim();
  if (!q) return;
  const m = q.match(/(\d{6})/);
  if (!m) { doSearch('w-search'); return; }
  try {
    const body = JSON.stringify({
      code: m[1], group: $('#w-group').value || '核心池',
      reason: $('#w-reason').value || '手动添加',
    });
    const r = await api('/api/watchlist?as_of=' + S.day,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    $('#w-search').innerHTML = `<span class="tag ok">已加入 ${esc(m[1])}（新增 ${r.added} 条）</span>`;
    loadWatch();
  } catch (e) { err('w-search', e); }
};

// ────────────────────────────────────────────── 每日推荐
// ★ 推荐一次要遍历 5359 只（实测 12s），点开页签再等 12s 是坏体验。
//   页面加载后就**后台预热**（缓存键=交易日），用户点开时通常已就绪。
//   ⚠ 用 fetch 直接打接口而不是调loadRecommend()——否则会往
//     「加载中…」的 DOM 里塞内容，用户切回来时看到的是旧提示。
function prewarmRecommend() {
  requestRecommend()
    .catch(() => { /* 预热失败无所谓，用户点开时会真算 */ });
}

function requestRecommend() {
  const day = S.day || '';
  if (S.recoPending && S.recoPendingDay === day) return S.recoPending;
  const pending = api('/api/recommend' + (day ? '?as_of=' + day : ''))
    .then(d => { S.reco = d; return d; })
    .finally(() => { if (S.recoPending === pending) S.recoPending = null; });
  S.recoPendingDay = day;
  S.recoPending = pending;
  return pending;
}

async function loadRecommend() {
  clearErr('rc-err');
  $('#rc-body').innerHTML =
    '<div class="note">正在计算全市场评分，首次加载通常需要十几秒，请稍候…</div>';
  try {
    const d = await requestRecommend();
    S.reco = d;
    renderRecommend(d);
  } catch (e) { $('#rc-body').innerHTML = ''; err('rc-err', e); }
}

function renderRecommend(d) {
  {
    if (!d.rows.length) { $('#rc-body').innerHTML = '<div class="note">' + esc(d.note || '无结果') + '</div>'; return; }
    const ip = d.image_policy;
    const policyNote = ip ? `<div class="card"><b>图片蒸馏策略 · 每日选股最高权重 ${(ip.weight * 100).toFixed(0)}%</b>
      <p>沿5日线回踩、缩量企稳、放量突破、相对强弱，并对追高和弱市扣分。可计算 ${ip.eligible_rows} 只。</p>
      <p>${esc(ip.note)} 启用日期 ${esc(ip.available_from)}；行情日期 ${esc(d.trade_date)}。</p>
      <details id="image-evidence"><summary>查看蒸馏规则与原始股票预测</summary><div id="image-evidence-body">展开后加载…</div></details></div>` : '';
    $('#rc-body').innerHTML = policyNote + `<table><thead><tr>
      <th class="l">代码</th><th class="l">名称</th><th class="l">档</th>
      <th class="num">分数</th><th class="num">图片策略</th><th class="num">收盘</th><th class="num">涨跌</th>
      <th class="num">仓位参考</th><th class="l">理由</th><th class="l">风险</th>
      </tr></thead><tbody>${d.rows.map((r) => `<tr data-c="${esc(r.code)}">
        <td class="l">${esc(r.code)}</td><td class="l">${esc(r.name)}</td>
        <td class="l"><span class="tag">${esc(r.tier)}</span></td>
        <td class="num">${num(r.score)}</td>
        <td class="num">${r.image_strategy?.enabled ? num(r.image_strategy.score) + '<br><small>贡献 ' + num(r.image_strategy.weighted_contribution) + ' · 覆盖 ' + (r.image_strategy.coverage * 100).toFixed(0) + '%</small>' : '—'}</td>
        <td class="num">${num(r.close)}</td>
        <td class="num ${cls(r.pct_chg)}">${pct(r.pct_chg)}</td>
        <td class="num">${pct(r.position_hint, 1)}</td>
        <td class="l">${esc((r.reasons || []).join('；'))}</td>
        <td class="l">${esc(r.risk_note || '')}</td>
      </tr>`).join('')}</tbody></table>
      <div class="note">
        输入 ${d.total_input} →硬性过滤后 ${d.after_filter}（剔除 ${d.rejected}）
        ｜档位分布 ${Object.entries(d.stats || {}).map(([k, v]) => k + '=' + v).join(' ')}
        ｜市场状态 ${esc(d.market_state)} · 仓位上限 ${pct(d.position_cap, 0)}${d.cached ? '（缓存）' : ''}<br>
        ${esc(d.note || '')}
        ${(d.rejections || []).length ? '<br>被剔除样例：' +
          d.rejections.slice(0, 8).map((x) => esc(x.code) + '(' +
            (x.reasons || []).join('/') + ')').join('、') : ''}
      </div>`;
    const evidence = $('#image-evidence');
    if (evidence) evidence.ontoggle = async () => {
      if (!evidence.open || evidence.dataset.loaded) return;
      try {
        const { catalog } = await api('/api/image-strategy');
        $('#image-evidence-body').innerHTML = '<p>来源：' + esc(catalog.author) + '；题材、财报及收益均按作者陈述记录，未独立验证。</p>' +
          catalog.rules.map(x => '<p><b>' + esc(x.id + ' ' + x.name) + '</b>：' + esc(x.interpretation) + '</p>').join('') +
          '<p>去重股票记录 ' + catalog.stock_forecasts.length + ' 条；历史案例不作股票白名单。</p>' +
          catalog.stock_forecasts.map(x => '<p>' + esc(x.date_label + ' · ' + x.code + ' ' + x.name + '：' + x.thesis + '；' + x.condition + '；' + x.kind) + '</p>').join('');
        evidence.dataset.loaded = '1';
      } catch (e) { $('#image-evidence-body').textContent = '读取出处失败：' + (e.detail || e.message || e); }
    };
    $('#rc-body').querySelectorAll('tr[data-c]').forEach((tr) => {
      tr.onclick = () => {
        $('#s-q').value = tr.dataset.c;
        $$('nav button').forEach((x) => x.classList.toggle('on', x.dataset.p === 'stock'));
        $$('section.page').forEach((x) => x.classList.toggle('on', x.id === 'p-stock'));
        queryStock();
      };
    });
  }
}

// ────────────────────────────────────────────── 个股详情
$('#s-go').onclick = queryStock;
$('#s-period').onchange = () => queryStock();
let stockRequest = 0, stockBusy = false;
async function queryStock(refresh = false) {
  // onclick passes an Event, not the background-refresh boolean.
  refresh = refresh === true;
  if (refresh && stockBusy) return;
  const code = $('#s-q').value.trim();
  if (!/^\d{6}$/.test(code)) { err('s-err', { detail: '请输入 6 位代码' }); return; }
  const period = $('#s-period').value;
  const request = ++stockRequest;
  stockBusy = true;
  clearErr('s-err'); clearErr('s-search');
  if (!refresh) $('#s-body').innerHTML = '<div class="note">加载中…</div>';
  try {
    const d = await api(period === 'daily' ? '/api/stock/' + code + (S.day ? '?as_of=' + S.day : '') :
      '/api/intraday/' + code + '?period=' + (period === 'line' ? 'm1' : period));
    if (request !== stockRequest || code !== $('#s-q').value.trim() || period !== $('#s-period').value) return;
    S.stock = d;
    if (period === 'daily') { setDay(d.trade_date, true); renderStock(d); }
    else { d.line = period === 'line'; renderIntraday(d); }
    doSearchFor('#s-search', code);
  } catch (e) {
    if (request !== stockRequest) return;
    $('#s-body').innerHTML = ''; err('s-err', e);
  } finally { if (request === stockRequest) stockBusy = false; }
}
async function doSearchFor(sel, code) {
  try {
    const d = await api('/api/search?q=' + code);
    const r = d.rows[0];
    $('#s-hint').textContent = r ? r.name : '';
  } catch (e) { /* 搜索失败不影响主流程 */ }
}
$('#s-add').onclick = async () => {
  const code = $('#s-q').value.trim();
  if (!/^\d{6}$/.test(code)) { err('s-search', { detail: '请输入 6 位代码' }); return; }
  try {
    const r = await api('/api/watchlist?as_of=' + S.day, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, reason: '个股页加入' }),
    });
    $('#s-search').innerHTML = `<span class="tag ok">已加入自选（新增 ${r.added} 条）</span>`;
    if ($('#s-period').value !== 'daily') queryStock();
  } catch (e) { err('s-search', e); }
};

function renderIntraday(d) {
  const states = {live:'盘中更新', stale:'行情过期或等待当日数据 · 暂停新增盘中提示', closed:'非交易时段 · 历史回看'};
  const bars = d.line ? d.bars.filter(b => b.d.slice(0,10) === d.trade_date) : d.bars.slice(-240);
  const signals = bars.length ? d.signals.filter(s => s.available_at >= bars[0].d && s.available_at <= bars[bars.length-1].d) : [];
  const selection = OpusIntradayMarkers.selectMarkers(signals, !!S.detailedMarkers, !!S.detailedMarkers && !!S.showInvalidated);
  $('#s-body').innerHTML = `<div class="card">
    <h2>${esc(d.code)} ${esc(d.name)} <small>${d.line ? '分时收盘价' : esc(d.period.slice(1)) + ' 分钟 K 线'} · ${esc(d.trade_date || '等待数据')}</small></h2>
    <div class="note" id="i-state">${esc(states[d.state])} · 来源：${esc(d.source)}<br>
      行情截至 ${esc(d.latest || '—')} · 最近抓取 ${esc(d.fetched_at || '—')} · 已完成 ${bars.length} 根<br>
      ${d.tracked ? (d.running ? '跟踪采集运行中；页面每 15 秒检查更新。' : '采集未运行，暂停新增提示。') : '该股未在自选或持仓中，加入自选后会自动采集。'}
      ${d.error || d.service_error ? '<br>采集异常：' + esc(d.error || d.service_error) : ''}</div>
    <div class="legend">B 买点观察 · S 卖点观察 · 实心＝规则确认 · 空心 ?＝候选 · 灰色 ×＝已失效 · R＝历史回放</div>
    ${d.image_policy?.enabled ? `<div class="note" id="i-policy">三图融合 ${esc(d.image_policy.version)}<br>${esc(d.image_policy.note)}<br>此处保留触发当时的观察记录，不代表该结构当前仍有效；灰色 × 也表示被开盘保护拦截。<br>${esc(d.image_policy.backtest?.note || '收益效果尚未证明。')}</div>` : ''}
    <label><input type="checkbox" id="i-detailed" ${S.detailedMarkers ? 'checked' : ''}> 全部观察模式（包含候选、辅助和重复触发）</label>
    <label><input type="checkbox" id="i-invalidated" ${S.showInvalidated ? 'checked' : ''} ${S.detailedMarkers ? '' : 'disabled'}> 同时显示失效／拦截记录</label>
    <div class="note" id="i-marker-count">当前 ${signals.length} 条记录 → 图上 ${selection.markers.length} 个标记。
      ${S.detailedMarkers ? '全部观察模式用于核查，密集标记不代表应反复买卖。' : '精简模式：仅显示已确认的主要观察；同一分钟同方向合并，同一半日连续同方向只留首个，反向确认后可再次标注。MACD减弱、尾盘下拐单独留在明细。'}
      图表合并只为便于阅读，未删除记录或修改交易规则及回测。</div>
    <div id="i-chart">${bars.length ? intradaySvg(bars, selection.markers, d.line) : '<div class="note">暂无已完成分钟线，正在等待采集。已有自选通常在首轮采集后显示。</div>'}</div>
    <div class="note">标记落在首次可计算的完成 K 线上，结构发生时间另列。R 仅代表当前历史样本逐根回放，不代表当时曾实时提示；盘中首次发现时间以表格记录为准。分时线使用一分钟收盘价，不含未完成分钟及成交均价。</div>
  </div><div class="card"><h2>买卖点观察记录 <small>当前图表区间 · ${signals.length} 条</small></h2>
    <div class="note">${esc(d.note)}</div>
    ${signals.length ? `<div style="overflow:auto"><table><thead><tr><th>观察点</th><th>状态</th><th>首次可计算 K 线</th><th>首次发现／回放计算</th><th>结构时间 / 价格</th><th>依据</th></tr></thead><tbody>${signals.slice().reverse().map(s => `<tr>
      <td>${esc(s.kind)}<br>${s.mode === 'live' ? '盘中记录' : '历史回放 R'}</td>
      <td>${!s.active ? (s.gate_reasons?.length ? '条件拦截 ×' : '已失效 ×') : s.status === 'candidate' ? '候选 ?' : '规则确认'}${d.state === 'stale' ? '<br>行情已过期' : ''}</td>
      <td>${esc(s.available_at)}<br>当时收盘 ${num(s.marker_price)}</td>
      <td>${esc(s.observed_at)}</td><td>${esc(s.structure_at)}<br>${num(s.price)}</td>
      <td class="l">${esc(s.reason)}<br>规则置信 ${num(s.confidence,2)}（非胜率）</td></tr>`).join('')}</tbody></table></div>` : '<div class="note">当前区间尚无符合规则的观察点；系统不会为了显示标记而生成信号。</div>'}
  </div>`;
  $('#i-invalidated').onchange = (event) => { S.showInvalidated=event.target.checked; renderIntraday(d); };
  $('#i-detailed').onchange = (event) => { S.detailedMarkers=event.target.checked; renderIntraday(d); };
}

function intradaySvg(bars, signals, line) {
  const W=1100,H=440,left=12,right=1015,top=30,bottom=310,volBottom=408;
  const hi=Math.max(...bars.map(b=>b.h)),lo=Math.min(...bars.map(b=>b.l));
  const pad=(hi-lo)*.14 || .1, y0=lo-pad,y1=hi+pad;
  const X=i=>left+(i+.5)*(right-left)/bars.length;
  const Y=p=>bottom-(p-y0)/(y1-y0)*(bottom-top);
  const width=Math.max(1,(right-left)/bars.length*.62);
  const maxV=Math.max(...bars.map(b=>b.v)) || 1;
  let svg=`<svg role="img" aria-label="${line ? '分时走势' : '分钟K线'}及买卖观察点" viewBox="0 0 ${W} ${H}" style="display:block;width:100%;background:#1a1f28;min-height:240px">`;
  for(let i=0;i<=4;i++) {
    const p=y0+(y1-y0)*i/4,y=Y(p);
    svg+=`<line x1="${left}" x2="${right}" y1="${y}" y2="${y}" stroke="#2c3444"/><text x="${right+8}" y="${y+4}" fill="#a7b2c6" font-size="12">${p.toFixed(2)}</text>`;
  }
  if(line) svg+=`<polyline points="${bars.map((b,i)=>X(i)+','+Y(b.c)).join(' ')}" fill="none" stroke="#7fb8f2" stroke-width="1.7"/>`;
  bars.forEach((b,i)=>{
    const x=X(i),col=b.c>=b.o?S.colors.up:S.colors.down;
    const title=esc(`${b.d} 开 ${b.o} 高 ${b.h} 低 ${b.l} 收 ${b.c} 量 ${fmtVol(b.v)}`);
    svg+=`<g><title>${title}</title>`;
    if(!line) svg+=`<line x1="${x}" x2="${x}" y1="${Y(b.h)}" y2="${Y(b.l)}" stroke="${col}"/><rect x="${x-width/2}" y="${Math.min(Y(b.o),Y(b.c))}" width="${width}" height="${Math.max(1,Math.abs(Y(b.c)-Y(b.o)))}" fill="${col}"/>`;
    else svg+=`<circle cx="${x}" cy="${Y(b.c)}" r="3" fill="transparent"/>`;
    const vh=b.v/maxV*65;
    svg+=`<rect x="${x-width/2}" y="${volBottom-vh}" width="${width}" height="${vh}" fill="${col}" opacity=".7"/></g>`;
    if(i % Math.max(1,Math.floor(bars.length/6))===0) svg+=`<text x="${Math.max(48,Math.min(right-40,x))}" y="432" fill="#a7b2c6" font-size="11" text-anchor="middle">${esc(b.d.slice(5,16))}</text>`;
  });
  const used={};
  signals.forEach(s=>{
    const i=bars.findIndex(b=>b.d>=s.available_at); if(i<0)return;
    const buy=/buy/i.test(s.kind),key=i+'-'+buy,offset=(used[key]||0)*15;used[key]=(used[key]||0)+1;
    const b=bars[i],x=X(i),y=buy?Y(b.l)+15+offset:Y(b.h)-15-offset;
    const col=!s.active?'#78818e':buy?'#67b1ff':'#ffbd6a';
    const label=(buy?'B':'S')+(s.mode==='replay'?'R':'')+(!s.active?'×':s.status==='candidate'?'?':'')+((s.members?.length || 1)>1 ? '·'+s.members.length : '');
    const path=buy?`M${x},${y-6}l-5,9h10Z`:`M${x},${y+6}l-5,-9h10Z`;
    svg+=`<g data-marker="${buy?'B':'S'}"><title>${esc((s.members || [s]).map(e=>e.kind+' '+e.available_at+' '+e.reason).join('\n'))}</title><path d="${path}" fill="${s.active&&s.status==='confirmed'?col:'none'}" stroke="${col}"/><text x="${x}" y="${y+(buy?17:-10)}" fill="${col}" font-size="11" text-anchor="middle">${label}</text></g>`;
  });
  return svg+`<text x="${right+8}" y="352" fill="#a7b2c6" font-size="11">${esc(fmtVol(maxV))}</text><text x="12" y="332" fill="#a7b2c6" font-size="11">成交量（股换算为手） · 时间轴按实际有数据的分钟排列</text></svg>`;
}

function renderStock(d) {
  const zs = d.last_zhongshu;
  const buys = d.signals.filter((s) => /buy|B1|B2|B3/i.test(s.kind));
  const sells = d.signals.filter((s) => /sell|S1|S2|S3/i.test(s.kind));

  $('#s-body').innerHTML = `
  <div class="card">
    <h2>${esc(d.code)} ${esc(d.name)}
      <small>${esc(d.trade_date)} · 位置「${esc(d.position)}」 ·
      笔方向「${esc(d.bi_direction || '—')}」 · ${d.bars.length} 根日线</small></h2>
    <div class="grid g4">
      <div class="kpi"><div class="lb">收盘</div>
        <div class="vl ${cls(d.bars.length ? d.bars[d.bars.length - 1].pct : null)}">
        ${num(d.bars.length ? d.bars[d.bars.length - 1].c : null)}</div>
        <div class="ex">当日 ${pct(d.bars.length ? d.bars[d.bars.length - 1].pct : null)}</div></div>
      <div class="kpi"><div class="lb">最后中枢</div>
        <div class="vl">${zs ? num(zs.low) + ' ~ ' + num(zs.high) : '无中枢'}</div>
        <div class="ex">${zs ? esc(zs.start) + ' → ' + esc(zs.end) : '样本不足'}</div></div>
      <div class="kpi"><div class="lb">中枢个数 / 信号数</div>
        <div class="vl">${d.zhongshus.length} / ${d.signals.length}</div>
        <div class="ex">当前日线样本内的结构信号，非盘中确认时间</div></div>
      <div class="kpi"><div class="lb">引擎参数</div>
        <div class="vl" style="font-size:14px">strict=${esc(d.params.strict_bi)}</div>
        <div class="ex">as_of 注入：${esc(d.trade_date)} 15:00</div></div>
    </div>
    <div class="legend">
      <span><i style="background:${S.colors.up}"></i>阳线（涨）</span>
      <span><i style="background:${S.colors.down}"></i>阴线（跌）</span>
      <span><i style="background:#4a90d9"></i>买·标注提醒</span>
      <span><i style="background:#9aa4b5"></i>卖·标注提醒（仅提醒，系统不自动卖出）</span>
    </div>
    <canvas id="kchart" height="360"></canvas>
    <canvas id="vchart" height="110"></canvas>
  </div>
  <div class="grid g2">
    <div class="card"><h2>缠论信号 <small>买点 / 卖点提醒</small></h2>
      <div id="s-sig">${renderSigs(buys, 'buy')}${renderSigs(sells, 'sell')}</div>
      <div class="note">${esc(d.note || '')}</div></div>
    <div class="card"><h2>中枢列表</h2>
      ${d.zhongshus.length ? `<table><thead><tr><th class="l">#</th>
        <th>下沿</th><th>上沿</th><th class="num">笔数</th><th class="l">区间</th>
        </tr></thead><tbody>${d.zhongshus.slice(-10).map((z) => `<tr>
        <td class="l">${z.idx}${z.is_last ? ' <span class="tag">最新</span>' : ''}</td>
        <td class="num">${num(z.low)}</td><td class="num">${num(z.high)}</td>
        <td class="num">${z.bi_count}</td>
        <td class="l">${esc(z.start)} → ${esc(z.end)}</td></tr>`).join('')}
        </tbody></table>` : '<div class="note">样本不足，未形成中枢。</div>'}
    </div>
  </div>`;
  // ★ 两张图共用同一套绘图区几何（AV），否则成交量柱与 K 线柱会竖向错位。
  //   几何只能算一次：resize 时画布宽度会变，但重算两遍必然漂移。
  //   ⚠ 传 `$('#kchart')` 而不是 `cv` —— cv 是 drawK 的局部变量，
  //     在这里写 cv 会得到 "cv is not defined"；且**异常被 catch 渲染成
  //     .err 块，console 依然干净** ⇒ 只看 console 的验收器会漏掉它。
  const AV = chartGeom($('#kchart'));
  drawK(d, AV);
  drawV(d, AV);
}

/** 计算 K 线与成交量图共用的绘图区几何。
 *
 * ★ 为什么要抽出来：右轴要留AX=52 像素，主图与副图**必须用同一个绘图宽**，
 *   否则同一根 bar 在两张图上的x 不同 ⇒ 视觉上「柱子对不上」。
 *   而且这是**几何常量**，只在 resize 时变，不该在两个函数里各写一遍字面量。
 */
function chartGeom(cv) {
  // 画布可能还没挂上（DOM 未渲染）⇒ 退回默认宽，而不是让调用方炸
  const W = (cv && cv.clientWidth) || 900;
  const AX = 52;             // 右侧价格轴预留
  return { W, AX, PW: W - AX - 8 };  // 8 = 左侧 pad
}

function renderSigs(list, kind) {
  if (!list.length) return '';
  const t = kind === 'buy' ? '买点提醒' : '卖点提醒（仅标注，不执行）';
  return `<div style="margin:6px 0;color:${kind === 'buy' ? '#4a90d9' : '#9aa4b5'}">${t}</div>` +
    list.map((s) => `<div class="sig"><b>${esc(s.kind)}</b>
      ${esc(s.date)} @ ${num(s.price)}
      <span class="tag">置信 ${num(s.confidence, 2)}</span><br>
      <span class="note" style="margin:0">${esc(s.reason || '')}</span></div>`).join('');
}

// ── K 线图（含缠论买卖点 + 中枢带）
function drawK(d, AV) {
  const cv = $('#kchart'); if (!cv) return;
  const bars = d.bars;
  const W = AV.W, H = 360;
  const pd = 8;
  const zs = d.zhongshus.filter((z) => z.end);
  const idxOf = (dt) => bars.findIndex((b) => b.d >= dt);
  // 图上只画最近 120 根，避免字挤在一起
  const from = Math.max(0, bars.length - 120);
  const vb = bars.slice(from);
  const off = from;

  const hi = Math.max(...vb.map((b) => b.h)), lo = Math.min(...vb.map((b) => b.l));
  const pad = (hi - lo) * 0.06 || 1;
  // ★ 右侧留出 52px 放价格轴 —— 之前标签写在 `x = W - pd + 2`，
  //   已越过 SVG 的 width 边界 ⇒ **整列被裁掉**，只剩残缺的「1」。
  //   几何由 chartGeom() 提供，与 drawV 共用（见 chartGeom 的说明）。
  const AX = AV.AX;
  const y0 = lo - pad, y1 = hi + pad;
  const PW = AV.PW;
  const X = (i) => pd + (i + 0.5) * ((PW - pd) / vb.length);
  const Y = (v) => H - pd - ((v - y0) / (y1 - y0)) * (H - pd * 2);
  const bw = Math.max(1, ((PW - pd) / vb.length) * 0.62);

  let g = '';
  // 网格 + 右轴价格
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4, y = Y(v);
    g += `<line x1="${pd}" y1="${y}" x2="${PW}" y2="${y}" stroke="#2c3444"/>` +
      `<text x="${PW + 4}" y="${y + 3.5}" fill="#8a93a3" font-size="10">${v.toFixed(2)}</text>`;
  }
  // 中枢带（画在K 线下方层）
  zs.forEach((z) => {
    const a = idxOf(z.start), b = idxOf(z.end);
    if (a < 0 || b < 0) return;
    const x1 = X(Math.max(0, a - off)), x2 = X(Math.min(vb.length - 1, b - off));
    const yy1 = Y(z.high), yy2 = Y(z.low);
    g += `<rect x="${x1}" y="${yy1}" width="${Math.max(1, x2 - x1)}"
      height="${Math.max(1, yy2 - yy1)}" fill="rgba(74,144,217,.13)"
      stroke="rgba(74,144,217,.5)" stroke-width="1"/>`;
  });
  // K 线：★涨红跌绿
  vb.forEach((b, i) => {
    const rise = (b.pct == null ? b.c >= b.o : b.pct >= 0);
    const col = rise ? S.colors.up : S.colors.down;
    const x = X(i);
    g += `<line x1="${x}" y1="${Y(b.h)}" x2="${x}" y2="${Y(b.l)}" stroke="${col}" stroke-width="1"/>`;
    const yo = Y(b.o), yc = Y(b.c);
    g += `<rect x="${x - bw / 2}" y="${Math.min(yo, yc)}" width="${bw}"
      height="${Math.max(1, Math.abs(yc - yo))}" fill="${col}"/>`;
  });
  // 买卖点标注：▲买 / ▼卖
  d.signals.forEach((s) => {
    const i = idxOf(s.date);
    if (i < 0 || i - off < 0 || i - off >= vb.length) return;
    const x = X(i - off), b = vb[i - off];
    const buy = /buy|B1|B2|B3/i.test(s.kind);
    const col = buy ? '#4a90d9' : '#9aa4b5';
    const y = buy ? Y(b.l) + 14 : Y(b.h) - 14;
    const tri = buy
      ? `M${x},${y - 7} L${x - 5},${y + 3} L${x + 5},${y + 3} Z`
      : `M${x},${y + 7} L${x - 5},${y - 3} L${x + 5},${y - 3} Z`;
    g += `<path d="${tri}" fill="none" stroke="${col}" stroke-width="1.6"/>
      <text x="${x}" y="${y + (buy ? 14 : -8)}" fill="${col}" font-size="9"
      text-anchor="middle">${buy ? 'B' : 'S'}</text>`;
  });
  // 横轴：每 30 根标一个日期
  vb.forEach((b, i) => {
    if (i % 30 !== 0) return;
    g += `<text x="${X(i)}" y="${H - 1}" fill="#8a93a3" font-size="9"
      text-anchor="middle">${b.d.slice(5)}</text>`;
  });
  cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#1a1f28'; ctx.fillRect(0, 0, W, H);
  ctx.font = '10px sans-serif';
  rasterize(ctx, g, W, H);
}

// 用离屏 SVG → Image 的方式统一渲染（含 <text>，canvas fillText 排版太啰嗦）
function rasterize(ctx, svg, W, H) {
  const img = new Image();
  const url = 'data:image/svg+xml;charset=utf-8,' +
    encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="' + W +
      '" height="' + H + '">' + svg + '</svg>');
  img.onload = () => { ctx.drawImage(img, 0, 0); };
  img.src = url;
}

// ── 成交量
function drawV(d, AV) {
  const cv = $('#vchart'); if (!cv) return;
  const bars = d.bars, W = AV.W, H = 110, pd = 8;
  const from = Math.max(0, bars.length - 120);
  const vb = bars.slice(from);
  const mx = Math.max(...vb.map((b) => b.v || 0)) || 1;
  // ★ 必须与 drawK 用**同一个绘图区宽度**，否则成交量柱与 K 线柱**竖向错位**
  //   （主图右侧留了 AX=52 的价格轴区，副图没留 ⇒ 同一根 bar 的 x 差半个身位）。
  //   AW 由 drawStock 统一算好后传入，避免两处各算一遍、日后再漂移。
  const PW = AV.PW, cw = (PW - pd) / vb.length;
  const X = (i) => pd + (i + 0.5) * cw;
  let g = `<line x1="${pd}" y1="${H - pd}" x2="${PW}" y2="${H - pd}" stroke="#2c3444"/>`;
  vb.forEach((b, i) => {
    const rise = (b.pct == null ? b.c >= b.o : b.pct >= 0);
    const h = (b.v || 0) / mx * (H - pd * 2);
    g += `<rect x="${X(i) - cw * 0.31}" y="${H - pd - h}"
      width="${Math.max(1, cw * 0.62)}" height="${h}"
      fill="${rise ? S.colors.up : S.colors.down}" opacity=".65"/>`;
  });
  // 副图右轴给一条量纲提示（不画刻度线，避免与主图轴重复造成视觉噪声）
  g += `<text x="${PW + 4}" y="12" fill="#8a93a3" font-size="10">${fmtVol(mx)}</text>`;
  g += `<text x="${pd}" y="12" fill="#8a93a3" font-size="10">成交量（最近 ${vb.length} 根，上限 ${fmtVol(mx)}）</text>`;
  cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#1a1f28'; ctx.fillRect(0, 0, W, H);
  rasterize(ctx, g, W, H);
}

// ────────────────────────────────────────────── 板块强度
async function loadBoards() {
  $('#bd-body').innerHTML = '<div class="note">加载中…</div>';
  try {
    const d = await api('/api/boards' + (S.day ? '?as_of=' + S.day : ''));
    if (!d.rows.length) { $('#bd-body').innerHTML = '<div class="note">当日无板块快照</div>'; return; }
    const mx = Math.max(...d.rows.map((r) => r.strength || 0)) || 1;
    $('#bd-body').innerHTML = `<table><thead><tr>
      <th class="l">板块</th><th class="l">类型</th><th class="num">强度</th><th class="l"></th>
      <th class="num">涨跌</th><th class="num">上涨占比</th><th class="num">净流入(万)</th>
      <th class="num">涨停</th><th class="num">最高连板</th><th class="num">因子覆盖</th><th class="l">算法</th>
      </tr></thead><tbody>${d.rows.map((r) => `<tr>
        <td class="l">${esc(r.name)} <span class="tag">${esc(r.code)}</span></td>
        <td class="l">${esc(r.type || '—')}</td>
        <td class="num">${num(r.strength, 1)}</td>
        <td class="l" style="width:110px"><div class="bar"><i style="width:${Math.max(0, Math.min(100, (r.strength || 0) / mx * 100))}%"></i></div></td>
        <td class="num ${cls(r.pct_chg)}">${pct(r.pct_chg)}</td>
        <td class="num">${pct(r.up_ratio, 1)}</td>
        <td class="num">${r.net_inflow == null ? '—' : Math.round(r.net_inflow).toLocaleString()}</td>
        <td class="num">${r.limit_up_cnt == null ? '—' : r.limit_up_cnt}</td>
        <td class="num">${r.max_streak == null ? '—' : r.max_streak + '板'}</td>
        <td class="num">${num(r.factor_coverage, 2)}</td>
        <td class="l"><span class="tag ${r.factor_coverage >= 0.99 ? '' : 'warn'}">${esc(r.score_method || '—')}</span></td>
      </tr>`).join('')}</tbody></table>
      <div class="note">
        八因子权重：${Object.entries(d.weights).map(([k, v]) => esc(k) + '=' + v).join('、')}<br>
        快照日 <b>${esc(d.trade_date)}</b>（板块表最新 ${esc(d.snapshot_latest)}），
        历史深度 <b>${d.n_history_days}</b> 天，带因子行 ${d.n_with_factor}/${d.n_total}。<br>
        ${d.fallback_reason ? '<span style="color:var(--warn)">已回退：' +
          esc(d.fallback_reason) + '</span><br>' : ''}
        ${esc(d.note || '')}
      </div>`;
  } catch (e) { $('#bd-body').innerHTML = ''; $('#bd-body').innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>'; }
}

// ────────────────────────────────────────────── 系统状态
async function loadHealth() {
  loadMailStatus();
  loadNewsSources();
  try {
    const d = await api('/api/health');
    setDay(d.latest_day, null);
    $('#h-task').innerHTML = `<table><thead><tr><th class="l">任务</th><th class="l">cron</th>
      <th class="l">说明</th></tr></thead><tbody>${d.tasks.map((t) => `<tr>
      <td class="l">${esc(t.name)}</td><td class="l"><code>${esc(t.cron)}</code></td>
      <td class="l">${esc(t.desc || '')}</td></tr>`).join('')}</tbody></table>
      <div class="note">共 ${d.n_tasks} 个调度任务</div>`;
    $('#h-data').innerHTML = `<div class="grid g3">
      <div class="kpi"><div class="lb">库内最新交易日</div><div class="vl">${esc(d.latest_day)}</div>
        <div class="ex">行情表 max(trade_date)</div></div>
      <div class="kpi"><div class="lb">可用交易日</div><div class="vl">${d.usable_days}</div>
        <div class="ex">门槛 ≥ ${d.universe_threshold} 只；最近可用 ${esc(d.latest_usable_day || '—')}</div></div>
      <div class="kpi"><div class="lb">语料 news</div><div class="vl">${d.n_news}</div>
        <div class="ex">${esc(d.role)}</div></div></div>`;
  } catch (e) { $('#h-task').innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>'; }
  // ★ LLM 面板与调度面板**独立 try**：LLM 不可用时不该让「调度任务」也变空白
  loadLLMPanel();
  // ★ 算力平台面板同样独立 —— 它走内网，不通是常态，
  //   绝不能因为它连不上就把这一页其它面板拖成错误条。
  loadPlatformPanel();
}

// ── 两江算力平台（只读探测）
async function loadPlatformPanel() {
  const el = $('#h-platform');
  if (!el) return;
  el.innerHTML = '<div class="note">探测中…</div>';
  let d;
  try { d = await api('/api/platform'); }
  catch (e) {
    el.innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>';
    return;
  }
  // ★ 三态分别提示：连不上 / 要重新登录 / 正常。
  //   混成一句「失败」会让用户以为系统坏了，而实际只是没连 VPN。
  const S_ = {
    ok: ['ok', '已连通'],
    unauthorized: ['bad', '登录态失效，需重新登录平台'],
    unreachable: ['warn', '网络不通（内网/VPN 未连）'],
    unconfigured: ['warn', '未配置'],
    unavailable: ['bad', '适配层未就绪'],
    error: ['bad', '探测出错'],
  }[d.state] || ['warn', d.state || '未知'];
  if (d.state !== 'ok') {
    el.innerHTML = `<div class="kpi"><div class="lb">${esc(S_[1])}</div>
      <div class="vl"><span class="tag ${S_[0]}">${esc(d.state)}</span></div>
      <div class="ex">${esc(d.message || '')}</div></div>`;
    return;
  }
  const qs = (d.quota_summary || []).map((q) => `<tr>
    <td class="l">${esc(q.name)}</td>
    <td class="num">${q.cpu}</td><td class="num">${q.mem_gb}</td>
    <td class="num">${q.gpu}</td></tr>`).join('');
  const tk = d.token || {};
  el.innerHTML = `<div class="grid g3">
    <div class="kpi"><div class="lb">连接状态</div>
      <div class="vl"><span class="tag ok">正常</span></div>
      <div class="ex">租户 ${esc(d.tenant_id || '—')}</div></div>
    <div class="kpi"><div class="lb">配额组</div><div class="vl">${d.quota_total}</div>
      <div class="ex">平台任务 ${d.task_total} 个</div></div>
    <div class="kpi"><div class="lb">token</div>
      <div class="vl">${tk.expire_at ? esc(tk.expire_at.slice(11, 16)) : '长期'}</div>
      <div class="ex">${tk.expire_at ? '剩余 ' + (tk.left_hours != null ? tk.left_hours + ' 小时' : '—') : esc(tk.note || '')}</div></div>
  </div>
  <table><thead><tr><th class="l">配额</th><th class="num">CPU</th>
    <th class="num">内存GB</th><th class="num">GPU</th></tr></thead>
    <tbody>${qs}</tbody></table>
  <div class="note">只读探测：不建任务、不改配额。探测于${esc(d.checked_at || '')}</div>`;
}

// ── LLM 连通性自检 + 用量成本（规格 2.4 要求 4/5）
async function loadLLMPanel() {
  const q = S.day ? '?as_of=' + S.day : '';
  try {
    const h = await api('/api/llm/health' + q);
    // ★ 状态用**中性绿/灰**，不用 .up/.down ——
    //   那两个类承载的是「涨红跌绿」的行情语义（配色铁律，全局不得反转）。
    //   把「正常」染成红色会让读者以为出了行情问题。
    const dot = (ok) => ok
      ? '<span class="ok">● 正常</span>' : '<span class="bad">● 异常</span>';
    $('#h-llm').innerHTML =
      `<div class="grid g3">
    <div class="kpi"><div class="lb">${esc(h.provider)} 服务</div>
      <div class="vl">${dot(h.reachable)}</div><div class="ex">${esc(h.hint || '')}</div></div>
    <div class="kpi"><div class="lb">模型</div>
      <div class="vl">${esc(h.model)}</div><div class="ex">${dot(h.model_loaded)} 模型已就绪</div></div>
    <div class="kpi"><div class="lb">真推理自检</div>
      <div class="vl">${h.probed ? dot(h.infer_ok) : '未执行'}</div>
      <div class="ex">${h.probed ? num(h.elapsed_ms, 0) + ' ms' : '需显式触发'}</div></div>
  </div>` +
      (h.err ? `<div class="warn">${esc(h.err)}</div>` : '') +
      `<div style="margin-top:10px"><button class="act" id="btn-probe">真发一次推理自检（约 1~2 秒）</button></div>
   <div id="probe-out"></div>`;
    $('#btn-probe').onclick = async () => {
      $('#probe-out').innerHTML = '<div class="note">推理中…</div>';
      try {
        const p = await api('/api/llm/health' + (q ? q + '&' : '?') + 'probe=true');
        $('#probe-out').innerHTML = `<div class="note">推理 ${p.infer_ok ? '成功' : '失败'}
          ，耗时 ${num(p.elapsed_ms, 0)} ms${p.err ? '：' + esc(p.err) : ''}</div>`;
      } catch (e) {
        $('#probe-out').innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>';
      }
    };
  } catch (e) {
    $('#h-llm').innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>';
  }

  try {
    const c = await api('/api/llm/status' + q);
    const t = c.totals;
    $('#h-cost').innerHTML = `<div class="grid g3">
      <div class="kpi"><div class="lb">窗口内调用</div><div class="vl">${t.n_calls}</div>
        <div class="ex">${esc(c.since)} ~ ${esc(c.as_of)}</div></div>
      <div class="kpi"><div class="lb">总 token</div>
        <div class="vl">${t.n_tokens.toLocaleString()}</div>
        <div class="ex">平均 ${num(t.avg_ms_per_call, 0)} ms/次</div></div>
      <div class="kpi"><div class="lb">估算成本</div><div class="vl">¥ ${num(t.cost_cny, 4)}</div>
        <div class="ex">${t.n_fallback} 次规则兜底（未调模型）</div></div>
    </div>` +
      (c.rows.length ? `<table><thead><tr>
        <th class="l">provider</th><th class="l">task</th><th class="num">次数</th>
        <th class="num">成功</th><th class="num">兜底</th><th class="num">输入tok</th>
        <th class="num">输出tok</th><th class="num">耗时s</th><th class="num">成本</th>
        </tr></thead><tbody>${c.rows.map((r) => `<tr>
        <td class="l">${esc(r.provider)}</td><td class="l">${esc(r.task)}</td>
        <td class="num">${r.n_calls}</td><td class="num">${r.n_ok}</td>
        <td class="num">${r.n_fallback}</td><td class="num">${r.prompt_tokens}</td>
        <td class="num">${r.completion_tokens}</td>
        <td class="num">${num(r.elapsed_ms / 1000, 1)}</td>
        <td class="num">${num(r.cost_cny, 4)}</td></tr>`).join('')}</tbody></table>` :
        '<div class="note">窗口内无调用记录。可运行 <code>scripts/parse_news.py --limit 10</code> 产生。</div>') +
      `<div class="note">${esc(c.cost_note)} ${esc(c.speed_note)}</div>`;
  } catch (e) {
    $('#h-cost').innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>';
  }
}

// ────────────────────────────────────────────── 启动
// ★ resize 时必须重算几何（clientWidth 会变），但仍只算一次再传给两张图
window.addEventListener('resize', () => {
  if (S.stock && !S.stock.period) {
    const AV = chartGeom($('#kchart'));
    drawK(S.stock, AV); drawV(S.stock, AV);
  }
});
loadOverview().then(prewarmRecommend);

// 金十采集与行情/模型状态独立，避免外部服务失败遮挡采集诊断。
async function loadNewsSources() {
  const el = $('#h-news');
  if (!el) return;
  try {
    const d = await api('/api/news/sources');
    const labels = {ok: '正常', degraded: '部分异常', error: '失败', unsupported: '尚未接入'};
    el.innerHTML = `<div class="note">金十 / 新浪后台采集：${d.running ? '运行中' : '未运行'}；每轮完成后 ${d.interval_seconds} 秒再次检查，关闭软件后停止。公开最新一页，不含付费正文。</div>` +
      `<table><thead><tr><th class="l">来源</th><th>状态</th><th>本次新增 / 重复</th><th>累计原文 / 待解析</th><th class="l">最近成功</th></tr></thead><tbody>` +
      d.sources.map(r => `<tr><td class="l">${esc(r.source)}</td><td>${esc(r.stale ? '状态已过期' : (labels[r.state] || r.state))}</td><td>${r.saved ?? '—'} / ${r.duplicates ?? '—'}</td><td>${r.raw_total} / ${r.pending}</td><td class="l">${esc(r.last_success || '—')}</td></tr>`).join('') + '</tbody></table>' +
      d.sources.map(r => `<div class="note">${esc(r.source)}：最近原文 ${esc(r.latest_item || '—')}；跳过锁定 ${r.skipped?.locked || 0}，广告 ${r.skipped?.ads || 0}，空内容 ${r.skipped?.empty || 0}，格式异常 ${r.skipped?.invalid || 0}${r.error ? '；' + esc(r.error) : ''}</div>`).join('') +
      '<details><summary>金十最近公开原文</summary>' + d.recent.map(r => `<p><small>${esc(r.ts)}</small><br>${esc(r.content || r.title || '')}</p>`).join('') + '</details>';
  } catch (e) { el.innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>'; }
}

async function loadTrackedNews() {
  const el = $('#w-news');
  if (!el) return;
  try {
    const d = await api('/api/news/tracked');
    const dirs = {bull: ['利好', 'up'], bear: ['利空', 'down'], neutral: ['中性', ''], unknown: ['待核实', 'warn']};
    const modelStates = {starting:'启动中', ok:'运行正常', idle:'等待新关联新闻', empty_pool:'重点股票池为空', model_unavailable:'本地模型未就绪，关联新闻待解析', error:'解析异常', not_running:'后台未运行'};
    const missing = d.stocks.filter(s => !s.industry && !s.concept);
    el.innerHTML = `<div class="note">重点跟踪 ${d.stocks.length} 只，其中持仓 ${d.stocks.filter(s => s.held).length} 只。持仓优先，同类股票中直接关联优先。待处理 ${d.pending} 条。<br>解析：${esc(modelStates[d.analysis?.state] || d.analysis?.state || '未知')}。标签为模型判断，需结合原文核实。</div>` +
      (missing.length ? `<div class="note">${esc(missing.map(s => s.name || s.code).join('、'))} 缺少行业/概念资料，目前按公司名称和股票代码匹配。</div>` : '') +
      (d.rows.length ? d.rows.map(r => {
        const direction = r.state === 'done' ? (dirs[r.direction] || dirs.unknown) : dirs.unknown;
        const relation = r.relation === 'direct' ? '直接提及' : (r.relation === 'source_link' ? '来源关联标的' : '行业/概念候选');
        return `<article class="card"><b>${esc(r.name)} ${esc(r.code)}</b> <span class="tag">${r.held ? '持仓' : '自选'}</span> <span class="tag ${direction[1]}">${direction[0]}</span> <span class="tag">${relation}</span>
          <p><small>${esc(r.source)} · ${esc(r.ts)} · 匹配：${esc(r.matched)}${r.confidence == null ? '' : ' · 置信度 ' + Math.round(r.confidence*100) + '%'}${r.strength == null ? '' : ' · 强度 ' + r.strength + '/5'}</small></p>
          <p>${esc(r.content || r.title || '')}</p>
          <div class="note">${esc(r.reason || '等待本地模型核实关联及影响方向')}${r.evidence ? '<br>原文依据：' + esc(r.evidence) : ''}${r.model ? '<br>模型：' + esc(r.model) : ''}</div></article>`;
      }).join('') : '<div class="note">当前已采集的最近七天新闻中，暂无匹配的关联新闻。公开快讯流并非个股新闻全量检索。</div>');
  } catch(e) { el.innerHTML = '<div class="err">' + esc(e.detail || e.message) + '</div>'; }
}
// 仅刷新正在显示的资讯面板，不触发昂贵的行情重算。
setInterval(() => {
  if (document.hidden) return;
  if ($('#p-watch')?.classList.contains('on')) loadTrackedNews();
  if ($('#p-health')?.classList.contains('on')) { loadNewsSources(); loadMailStatus(); }
}, 30000);

setInterval(() => {
  if (!document.hidden && $('#p-stock')?.classList.contains('on') && $('#s-period').value !== 'daily') queryStock(true);
}, 15000);

async function loadMailStatus() {
  const el = $('#h-mail');
  if (!el) return;
  try {
    const d = await api('/api/mail/status');
    const labels = {pending:'待发送',retry:'等待重试',sending:'发送中',sent:'服务器已接受',partial:'部分收件人拒收',uncertain:'结果待核对',failed:'失败',expired:'已过期',cancelled:'已取消'};
    const beatAge = d.heartbeat ? (Date.now()-Date.parse(d.heartbeat.at))/1000 : null;
    const state = !d.enabled ? '未启用' : !d.running ? '调度未运行' : beatAge == null ? '启动中' : beatAge > 180 ? '心跳过期' : '运行中';
    el.innerHTML = `<div class="note">${state} · 最近检查 ${esc(d.heartbeat?.at || '—')}<br>交易日 09:00 盘前、12:00 午间、15:05 收盘；周六 10:00 周报。需要软件保持运行，最小化到托盘可继续。</div>
      <div class="note">持仓重要利好/利空：强度 ≥3、置信度 ≥70%；普通告警交易时段发送，重大利空（强度5、置信度≥80%）即时提醒。自选股仅在界面标记。重启不补发历史报告。</div>` +
      (d.error ? `<div class="err">${esc(d.error)}</div>` : '') +
      `<table><thead><tr><th>类型</th><th>状态</th><th>最近尝试</th><th>次数</th><th>说明</th></tr></thead><tbody>` +
      d.recent.map(r => `<tr><td>${esc(r.kind)}</td><td>${esc(labels[r.state] || r.state)}</td><td>${esc(r.attempted_at || r.created_at)}</td><td>${r.attempts}</td><td>${esc(r.error || '')}</td></tr>`).join('') +
      '</tbody></table><div class="note">“服务器已接受”不等于已进入收件箱。结果待核对的邮件不会自动重复发送。</div>';
  } catch(e) { el.innerHTML='<div class="err">'+esc(e.detail || e.message)+'</div>'; }
}

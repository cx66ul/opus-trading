/** 壳层逻辑实测：不需要 Electron 二进制，只验纯逻辑。 */
'use strict';
const fs = require('fs');
const path = require('path');

const HTML = fs.readFileSync(path.join(__dirname, '..', 'shell.html'), 'utf-8');
const JS   = fs.readFileSync(path.join(__dirname, '..', 'shell.js'), 'utf-8');

let fails = 0;
const check = (name, cond, extra) => {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`); fails++; }
};

console.log('=== 1) 四个屏都要有 ===');
for (const id of ['scr-boot', 'scr-error', 'scr-wizard']) {
  check(`#${id} 存在`, HTML.includes(`id="${id}"`));
}

console.log('=== 2) 向导三态都有对应样式 ===');
const css = fs.readFileSync(path.join(__dirname, '..', 'shell.css'), 'utf-8');
for (const lv of ['pass', 'skip', 'block']) {
  check(`.tag.${lv} 有样式`, css.includes(`.tag.${lv}`));
}

console.log('=== 3) 失败路径不白屏 ===');
check('有「重试」按钮', HTML.includes('btn-retry'));
check('有「复制诊断」按钮', HTML.includes('btn-copy'));
check('有技术细节折叠区', HTML.includes('err-detail'));

console.log('=== 4) bridge 缺失时的降级 ===');
check('检测 window.hawk 存在', JS.includes('window.hawk'));
check('缺失时显示错误而非静默', /showError[\s\S]{0,200}window\.hawk is undefined/.test(JS));
// ★ 每个按钮回调都要有 null 守卫。
//   按**行块**判定（从 addEventListener 到下一个 addEventListener 之间），
//   不能用固定字符窗口 —— 守卫前若有注释，窗口就够不到。
const lines = JS.split('\n');
const blocks = [];
let cur = null;
for (const ln of lines) {
  if (/addEventListener\('click'/.test(ln)) { cur = [ln]; blocks.push(cur); }
  else if (cur) cur.push(ln);
}
const unguarded = blocks.filter(
  (b) => !b.join('\n').includes('if (!bridge)'));
check('所有按钮回调都有 bridge 守卫', unguarded.length === 0,
  `无守卫的回调 ${unguarded.length} 个：`
  + unguarded.map((b) => b[0].trim()).join(' | '));
check('按钮回调数量 > 0', blocks.length >= 4, `实际 ${blocks.length} 个`);

console.log('=== 5) 阻断项必须拦住「进入应用」 ===');
check('按 level==block 禁用按钮', JS.includes("level === 'block'"));

console.log('=== 6) 修复动作原样展示（不吞成一句话） ===');
check('渲染 fix 到 <code>', JS.includes('c.fix'));
check('HTML 转义', JS.includes('function esc'));

console.log('=== 7) 后端就绪才跳主界面 ===');
check('按 state==running 跳转', JS.includes("state === 'running'"));
check('按 base_url 跳转', JS.includes('be.base_url'));
check('启动失败不跳转', JS.includes("state === 'failed'"));

console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过');
process.exit(fails ? 1 : 0);

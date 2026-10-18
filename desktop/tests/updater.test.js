'use strict';
/**
 * 更新器实测脚本（不进pytest —— 桌面端逻辑用 node 直接验）
 * 用法：node tests/updater.test.js
 */
const path = require('path');
const fs = require('fs');
const os = require('os');
const http = require('http');
const crypto = require('crypto');

const { Updater } = require('../updater.js');

let fails = 0;
function check(name, cond, extra) {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`); fails++; }
}

function listen(srv) {
  return new Promise((res) => srv.listen(0, '127.0.0.1',
    () => res(srv.address().port)));
}

(async () => {
  const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-upd-'));

  // ── 1) 版本比较：字符串比较会把 '0.10.0' 判成小于 '0.9.0'
  console.log('=== 版本比较 ===');
  const T = [
    ['0.10.0', '0.9.0', true],     // ★ 字符串比较会错
    ['0.2.0', '0.10.0', false],
    ['1.0.0', '1.0.0', false],
    ['1.0.1', '1.0.0', true],
    ['0.3.0', '0.2.9', true],
    ['2.0', '1.99.99', true],
    ['0.10.0', '0.9.9', true],
  ];
  for (const [a, b, exp] of T) {
    check(`${a} > ${b}`, Updater.isNewer(a, b) === exp,
      `得到 ${Updater.isNewer(a, b)}`);
  }

  // ── 2) sha256 正确 → 放行
  const body = Buffer.from('fake-opus-package-bytes');
  const sha = crypto.createHash('sha256').update(body).digest('hex');
  let port;
  const srv = http.createServer((req, res) => {
    if (req.url === '/pkg.zip') {
      res.writeHead(200, { 'Content-Length': body.length });
      res.end(body);
    } else if (req.url === '/manifest.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ version: '0.3.0',
        url: `http://127.0.0.1:${port}/pkg.zip`, sha256: sha,
        notes: 'test', size: body.length }));
    } else { res.writeHead(404); res.end(); }
  });
  port = await listen(srv);

  console.log('=== 校验通过时应放行 ===');
  const u = new Updater({ root: ROOT, version: '0.2.0',
    manifestUrl: `http://127.0.0.1:${port}/manifest.json` });
  const ck = await u.check();
  check('check 返回 ok', ck.ok === true);
  check('识别为新版本', ck.available === true);
  const ap = await u.apply();
  check('apply 成功', ap.ok === true, ap.message);
  check('文件已落盘', ap.ok && fs.existsSync(ap.file));

  // ── 3) sha256 不匹配 → 必须拒绝
  console.log('=== sha256 不匹配必须拒绝 ===');
  let p2;
  const srv2 = http.createServer((req, res) => {
    if (req.url === '/pkg.zip') { res.writeHead(200); res.end(body); }
    else if (req.url === '/m.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ version: '0.3.0',
        url: `http://127.0.0.1:${p2}/pkg.zip`, sha256: '0'.repeat(64) }));
    } else { res.writeHead(404); res.end(); }
  });
  p2 = await listen(srv2);
  const u2 = new Updater({ root: ROOT, version: '0.2.0',
    manifestUrl: `http://127.0.0.1:${p2}/m.json` });
  const r2 = await u2.apply();
  check('拒绝安装', r2.ok === false);
  check('错误信息含「校验不通过」', /校验不通过/.test(r2.message || ''));

  // ── 4) manifest 无 sha256 → 必须拒绝（宁可不更新）
  console.log('=== manifest 无 sha256 必须拒绝 ===');
  let p3;
  const srv3 = http.createServer((req, res) => {
    if (req.url === '/pkg.zip') { res.writeHead(200); res.end(body); }
    else if (req.url === '/m.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ version: '0.3.0',
        url: `http://127.0.0.1:${p3}/pkg.zip` }));
    } else { res.writeHead(404); res.end(); }
  });
  p3 = await listen(srv3);
  const u3 = new Updater({ root: ROOT, version: '0.2.0',
    manifestUrl: `http://127.0.0.1:${p3}/m.json` });
  const r3 = await u3.apply();
  check('拒绝未校验的包', r3.ok === false);
  check('错误信息提到 sha256 缺失', /sha256/.test(r3.message || ''));

  // ── 5) 下载中断 → 不得留下 .part 被当成完整包
  console.log('=== 下载失败不留残包 ===');
  const srv4 = http.createServer((req, res) => {
    if (req.url === '/pkg.zip') {
      // 发一半就断
      res.writeHead(200, { 'Content-Length': body.length * 2 });
      res.write(body);
      setTimeout(() => res.destroy(), 30);
    } else if (req.url === '/m.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ version: '0.3.0',
        url: `http://127.0.0.1:${p4}/pkg.zip`, sha256: sha }));
    } else { res.writeHead(404); res.end(); }
  });
  let p4; p4 = await listen(srv4);
  const u4 = new Updater({ root: ROOT, version: '0.2.0',
    manifestUrl: `http://127.0.0.1:${p4}/m.json` });
  const r4 = await u4.apply();
  const parts = fs.readdirSync(path.join(ROOT, 'updates'))
    .filter((f) => f.endsWith('.part'));
  check('下载中断被识别为失败', r4.ok === false);
  check('无残留 .part 文件', parts.length === 0, `留下 ${parts}`);

  // ── 6) 每次动作都要落日志（不能只在 UI 闪一下）
  console.log('=== 日志落盘 ===');
  const log = path.join(ROOT, 'data', 'update.log');
  check('update.log 已生成', fs.existsSync(log));
  const lines = fs.readFileSync(log, 'utf-8').trim().split('\n');
  check('日志含多条记录', lines.length >= 3, `实际 ${lines.length} 行`);
  check('日志含校验不通过事件',
    lines.some((l) => /verify_mismatch/.test(l)));

  srv.close(); srv2.close(); srv3.close(); srv4.close();
  fs.rmSync(ROOT, { recursive: true, force: true });
  console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过');
  process.exit(fails ? 1 : 0);
})();

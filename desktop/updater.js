'use strict';
/**
 * Hawk · 自动更新
 * ====================================================
 *
 * ★★ 为什么不用 electron-updater
 * ---------------------------------------------
 * 1) 它默认从 GitHub Releases 拉更新，而本项目是**内网/离线部署**为主
 * 2) 它的「校验」依赖远端 manifest 里的 sha256，**本地文件源不生效**
 * 3) 出问题时错误信息埋在它内部，看不到到底校验了哪个文件
 * ⇒ 需求就是「读本地 manifest → 下载 → 校验 sha256 → 应用」，
 *   200 行能写完，且每一步可单独测试。
 *
 * ★★ 死规矩：sha256 不匹配 ⇒ 拒绝安装，且**保留旧版**
 * ---------------------------------------------
 * 自动更新最危险的失败模式是「下到一个坏包然后覆盖了能用的版本」，
 * 而用户只有在新版启动失败后才发现，且往往已经无法回退。
 * ⇒ 校验必须在**解压/替换之前**完成。
 * ⇒ 校验失败必须落一条记录，不能只在 UI 上闪一下。
 */

const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const crypto = require('crypto');
const { URL } = require('url');

function sha256File(p) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256');
    const s = fs.createReadStream(p);
    s.on('data', (d) => h.update(d));
    s.on('end', () => resolve(h.digest('hex')));
    s.on('error', reject);
  });
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https:') ? https : http;
    lib.get(url, { timeout: 15000 }, (res) => {
      if (res.statusCode !== 200) {
        reject(new Error(`manifest HTTP ${res.statusCode}`));
        res.resume();
        return;
      }
      let b = '';
      res.on('data', (d) => { b += d; });
      res.on('end', () => {
        try { resolve(JSON.parse(b)); }
        catch (e) { reject(new Error('manifest 不是合法 JSON：' + e.message)); }
      });
    }).on('error', reject);
  });
}

function download(url, dest, onProgress) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https:') ? https : http;
    const tmp = dest + '.part';
    const out = fs.createWriteStream(tmp);
    let got = 0;
    let settled = false;
    const done = (fn, arg) => { if (!settled) { settled = true; fn(arg); } };
    const cleanup = () => {
      try { out.close(); } catch (e) { /* 已关 */ }
      try { if (fs.existsSync(tmp)) fs.unlinkSync(tmp); } catch (e) { /* 无 */ }
    };

    const req = lib.get(url, { timeout: 60000 }, (res) => {
      if (res.statusCode !== 200) {
        cleanup();
        done(reject, new Error(`下载 HTTP ${res.statusCode}`));
        return;
      }
      const total = parseInt(res.headers['content-length'] || '0', 10);

      //★★★ 必须校验「实际收了多少」vs「声明要多少」
      //   实测（2026-10-11）：服务端发一半就 destroy() 时，
      //   客户端**仍会触发 `'end'`** ⇒ 旧实现直接 rename
      //   `.part` → 正式文件，而那个文件其实不存在
      //   ⇒ 抛 ENOENT，且是**未捕获异常** ⇒ 整个 Electron 主进程崩掉。
      //   自动更新失败把程序搞崩，是最坏的失败模式。
      res.on('data', (d) => { got += d.length; out.write(d);
        if (onProgress) onProgress(got, total); });
      res.on('end', () => {
        if (total && got < total) {
          cleanup();
          done(reject, new Error(
            `下载不完整：收到 ${got}/${total} 字节`));
          return;
        }
        out.close(() => {
          try {
            fs.renameSync(tmp, dest);
            done(resolve, { bytes: got });
          } catch (e) {
            cleanup();
            done(reject, e);
          }
        });
      });
      res.on('error', (e) => { cleanup(); done(reject, e); });
      // ★ 对端提前断开（destroy/aborted）
      res.on('aborted', () => {
        cleanup();
        done(reject, new Error(`下载中断：收到 ${got}/${total || '?'} 字节`));
      });
    });
    req.on('error', (e) => { cleanup(); done(reject, e); });
  });
}

class Updater {
  constructor({ root, version, manifestUrl }) {
    this.root = root;
    this.version = version;
    // ★ 默认读**本地** manifest（离线/内网部署）
    this.manifestUrl = manifestUrl
      || 'file:///' + path.join(root, 'updates', 'manifest.json')
         .replace(/\\/g, '/');
    this.cacheDir = path.join(root, 'updates');
    this.logFile = path.join(root, 'data', 'update.log');
  }

  _log(obj) {
    const line = JSON.stringify(Object.assign(
      { ts: new Date().toISOString(), from: this.version }, obj));
    try {
      fs.mkdirSync(path.dirname(this.logFile), { recursive: true });
      fs.appendFileSync(this.logFile, line + '\n', 'utf-8');
    } catch (e) { /* 日志失败不阻塞主流程 */ }
    return line;
  }

  /** 比较版本：'0.2.0' vs '0.10.0' 必须按数字比而非字符串比。 */
  static isNewer(remote, local) {
    const a = String(remote).split('.').map((x) => parseInt(x, 10) || 0);
    const b = String(local).split('.').map((x) => parseInt(x, 10) || 0);
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const d = (a[i] || 0) - (b[i] || 0);
      if (d > 0) return true;
      if (d < 0) return false;
    }
    return false;
  }

  /** 只检查，不下载。 */
  async check() {
    let m;
    try {
      m = await getJson(this.manifestUrl);
    } catch (e) {
      const line = this._log({ ev: 'check_failed', err: String(e.message) });
      return { ok: false, available: false,
               message: `无法读取更新清单：${e.message}`, log: line };
    }
    if (!m || !m.version || !m.url) {
      const line = this._log({ ev: 'manifest_invalid', manifest: m });
      return { ok: false, available: false,
               message: '更新清单缺 version 或 url 字段', log: line };
    }
    const newer = Updater.isNewer(m.version, this.version);
    this._log({ ev: 'check', remote: m.version, newer });
    return {
      ok: true, available: newer,
      current: this.version, latest: m.version,
      notes: m.notes || '', sha256: m.sha256 || '',
      size: m.size || 0, url: m.url,
    };
  }

  /** 下载 + 校验。★ 校验不过绝不替换。 */
  async apply(version) {
    const info = await this.check();
    if (!info.ok || !info.available) {
      return { ok: false, message: info.message || '已是最新版本' };
    }
    if (version && version !== info.latest) {
      return { ok: false, message: `版本不一致：请求 ${version}，清单 ${info.latest}` };
    }
    const dest = path.join(this.cacheDir, `hawk-${info.latest}.zip`);
    fs.mkdirSync(this.cacheDir, { recursive: true });

    let dl;
    try {
      dl = await download(info.url, dest);
    } catch (e) {
      const line = this._log({ ev: 'download_failed', ver: info.latest,
                               err: String(e.message) });
      return { ok: false, message: `下载失败：${e.message}`, log: line };
    }

    // ★★★ 校验：manifest 没给 sha256 ⇒ 拒绝（宁可不更新）
    if (!info.sha256) {
      this._log({ ev: 'verify_skipped_no_sha256', ver: info.latest });
      return { ok: false,
               message: '更新清单未提供 sha256，拒绝安装未校验的包' };
    }
    const got = await sha256File(dest);
    if (got.toLowerCase() !== String(info.sha256).toLowerCase()) {
      const line = this._log({ ev: 'verify_mismatch', ver: info.latest,
                               expect: info.sha256, got });
      // ★ 保留已下载文件供排查，但**不替换任何东西**
      return { ok: false, message:
        `sha256 校验不通过，已保留旧版。\n  期望 ${info.sha256}\n  实际 ${got}\n  文件：${dest}`,
        log: line };
    }
    this._log({ ev: 'verified', ver: info.latest, bytes: dl.bytes });
    return { ok: true, file: dest, version: info.latest, bytes: dl.bytes,
             message: '下载并校验通过，重启后应用' };
  }
}

module.exports = { Updater, sha256File };

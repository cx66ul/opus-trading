'use strict';
/**
 * Opus 辅助交易 · 预加载脚本
 * ====================================================
 *
 * ★ 为什么必须存在这个文件，而不是让渲染进程直接 require('electron')
 * ---------------------------------------------
 * `webPreferences.contextIsolation: true` 时，渲染进程里的 `require` 被禁用。
 * 于是前端若要调主进程能力（重启后端、查状态、检查更新），
 * **唯一**的通道就是 preload 里 `contextBridge` 暴露的接口。
 *
 * ★ 暴露面要**最小**
 *   只暴露 5 个具名函数，不暴露 ipcRenderer 本身
 *   —— 暴露了它 = 渲染进程可以调任意 IPC 通道，
 *   等于把主进程的权限全部放开。
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('hawk', {
  /** 后端状态快照（state/base_url/pid/stderr_tail…） */
  backendStatus: () => ipcRenderer.invoke('backend:status'),
  /** 重启后端 */
  restartBackend: () => ipcRenderer.invoke('backend:restart'),
  /** 首次运行向导状态 */
  wizardState: () => ipcRenderer.invoke('wizard:state'),
  /** 标记向导已完成 */
  finishWizard: () => ipcRenderer.invoke('wizard:finish'),
  /** 应用信息（版本/路径/端口） */
  appInfo: () => ipcRenderer.invoke('app:info'),
  /** 检查更新 */
  checkUpdate: () => ipcRenderer.invoke('update:check'),
  /** ★ 启动状态推送（主进程 → 渲染进程，单向） */
  onBoot: (cb) => {
    ipcRenderer.on('boot-status', (_e, payload) => cb(payload));
  },
});

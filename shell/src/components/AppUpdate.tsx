import { Download, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { act, useStore } from '../lib/store';
import { Glass, SettingRow } from './ui';

const RELEASES = 'https://github.com/Francis-Zxp/DotVoice/releases';

export function AppUpdateBanner() {
  const update = useStore((s) => s.appUpdate);
  const phase = useStore((s) => s.state.phase);
  if (!update?.available || update.dismissed || ['starting', 'recording', 'processing'].includes(phase)) return null;
  const dismiss = async () => {
    await act('dismiss_update', { version: update.latest_version });
  };
  return (
    <div className="banner" role="status" data-app-update>
      <span className="banner-ico" data-tone="ok"><Download size={15} /></span>
      <span className="flex-1">点声 {update.latest_version} 已发布。下载后从托盘退出点声，再安装更新；现有设置和笔记会保留。</span>
      <button className="btn btn-sm" onClick={() => act('open_url', { url: update.download_url })}>下载更新</button>
      <button className="btn btn-ghost btn-sm" onClick={() => act('open_url', { url: update.release_url })}>更新说明</button>
      <button className="btn btn-ghost btn-sm" onClick={dismiss}>稍后</button>
    </div>
  );
}

export function AppUpdateCard() {
  const update = useStore((s) => s.appUpdate);
  const version = useStore((s) => s.version);
  const [checking, setChecking] = useState(false);
  async function check() {
    if (checking) return;
    setChecking(true);
    try {
      await act('check_updates', { force: true });
    } finally { setChecking(false); }
  }
  const status = checking ? '正在检查 GitHub 发行版…'
    : update?.status === 'error' ? update.error || '检查失败，请稍后重试。'
    : update?.available ? `发现新版本 ${update.latest_version}。`
    : update?.status === 'current' ? '当前已是最新正式版。' : '运行时自动检查更新，也可以手动检查。';
  return (
    <Glass className="px-6 py-1" data-update-settings>
      <SettingRow icon={<Download size={17} />} title={`点声 ${version}`} description={status}>
        <button className="btn btn-sm" disabled={checking} onClick={check}>
          <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />{checking ? '正在检查' : '检查更新'}
        </button>
        <button className="btn btn-sm" onClick={() => act('open_url', { url: update?.available ? update.download_url : RELEASES })}>
          {update?.available ? '下载更新' : '发行页面'}
        </button>
      </SettingRow>
      <p className="faint pb-4 text-[12px]">每 6 小时检查一次正式版。下载不会中断录音；安装前需从托盘退出点声。0.11.4 及更早版本需手动安装一次新版。</p>
    </Glass>
  );
}

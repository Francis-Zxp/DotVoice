import { AnimatePresence, motion, MotionConfig } from 'motion/react';
import { Loader2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { CoreBanner, PAGES, ReminderBanner, Sidebar, TitleBar, Toasts, type Page } from './components/Chrome';
import { Boundary } from './components/Boundary';
import { AppUpdateBanner } from './components/AppUpdate';
import { SetupWizard } from './components/SetupWizard';
import { ease, LiquidDefs } from './components/ui';
import { call, isMock } from './lib/bridge';
import { connectCore, useStore } from './lib/store';
import { BatteryPage } from './pages/BatteryPage';
import { DevicesPage } from './pages/DevicesPage';
import { NotesPage } from './pages/NotesPage';
import { SettingsPage } from './pages/SettingsPage';
import { VoicePage } from './pages/VoicePage';

export function useAppearance() {
  const appearance = useStore((s) => s.prefs?.appearance);
  useEffect(() => {
    const root = document.documentElement;
    if (!appearance || appearance.theme === 'system') delete root.dataset.theme;
    else root.dataset.theme = appearance.theme;
    root.classList.toggle('reduced', !!appearance?.reduced_motion);
  }, [appearance]);
  return !!appearance?.reduced_motion;
}

export function App() {
  const [page, setPage] = useState<Page>('voice');
  const ready = useStore((s) => s.ready);
  const mode = useStore((s) => s.mode);
  const setupPending = useStore((s) => s.prefs?.setup?.done === false && !s.smoke);
  const reduced = useAppearance();
  const main = useRef<HTMLElement>(null);
  const autoWizard = useRef(false);

  // A fresh install opens the setup wizard once per launch (an existing install never does).
  useEffect(() => {
    if (ready && setupPending && !autoWizard.current) {
      autoWizard.current = true;
      useStore.getState().openWizard();
    }
  }, [ready, setupPending]);

  useEffect(() => {
    connectCore();
    document.documentElement.classList.toggle('preview', isMock);
    const visibility = () => call('set_visible', { visible: !document.hidden }).catch(() => {});
    document.addEventListener('visibilitychange', visibility);
    return () => document.removeEventListener('visibilitychange', visibility);
  }, []);

  // Ctrl+1…5 jumps between pages, Ctrl+, opens settings (not while typing).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.ctrlKey || e.altKey || e.metaKey) return;
      const typing = (e.target as HTMLElement)?.closest?.('input, textarea, [contenteditable="true"]');
      const index = Number(e.key) - 1;
      if (index >= 0 && index < PAGES.length && !typing) { e.preventDefault(); setPage(PAGES[index].id); }
      else if (e.key === ',' && !typing) { e.preventDefault(); setPage('settings'); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const fill = page === 'notes' || page === 'voice';
  return (
    <MotionConfig reducedMotion={reduced ? 'always' : 'user'}>
      <LiquidDefs />
      <div className="window" data-mode={mode}>
        {(['input', 'notes', 'present'] as const).map((m) => <i key={m} className={`amb mode-${m}`} data-on={mode === m} aria-hidden="true" />)}
        <TitleBar />
        <div className="flex min-h-0 min-w-0">
          <Sidebar page={page} onPage={setPage} />
          <main ref={main} className={`scroll main ${fill ? 'main-fill' : ''}`} id="main">
            <CoreBanner />
            <ReminderBanner />
            <AppUpdateBanner />
            {!ready ? (
              <div className="faint grid h-full place-items-center">
                <span className="flex items-center gap-2 text-[13px]"><Loader2 size={15} className="animate-spin" />正在连接点声核心…</span>
              </div>
            ) : (
              <AnimatePresence mode="wait" initial={false} onExitComplete={() => main.current?.scrollTo({ top: 0 })}>
                <motion.div key={page} className={fill ? 'page-fill' : 'page'}
                  initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, transition: { duration: .09, ease: 'easeIn' } }}
                  transition={{ duration: .3, ease }}>
                  <Boundary name={page}>
                    {page === 'voice' && <VoicePage onNotes={() => setPage('notes')} onDevices={() => setPage('devices')} />}
                    {page === 'notes' && <NotesPage />}
                    {page === 'devices' && <DevicesPage onSettings={() => setPage('settings')} />}
                    {page === 'battery' && <BatteryPage />}
                    {page === 'settings' && <SettingsPage />}
                  </Boundary>
                </motion.div>
              </AnimatePresence>
            )}
          </main>
        </div>
        {ready && <Boundary name="wizard"><SetupWizard /></Boundary>}
        <Toasts />
      </div>
    </MotionConfig>
  );
}

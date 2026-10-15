/* The only path to the core. In a plain browser (design preview / QA) a mock
   stands in; it never touches devices and is clearly labelled in the UI. */
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { createMock } from './mock';
import type { ElevationStatus, OverlayEnvelope, OverlayLayout, OverlayMonitor } from './types';

type Handler = (event: string, data: any) => void;

export const inTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
const mock = inTauri ? null : ((window as any).__DOTVOICE_MOCK__ ?? createMock());

export class CoreError extends Error {}

export async function call<T = any>(method: string, params?: Record<string, unknown>): Promise<T> {
  const value = inTauri
    ? await invoke<T>('core_call', { method, params: params ?? null })
    : await mock.call(method, params ?? {});
  if (value && typeof value === 'object' && 'error' in (value as any) && (value as any).error) {
    throw new CoreError(String((value as any).error));
  }
  return value as T;
}

export function onCore(handler: Handler): () => void {
  if (!inTauri) return mock.on(handler);
  let stop: (() => void) | null = null;
  let cancelled = false;
  listen<{ event: string; data: any }>('core://event', (e) => handler(e.payload.event, e.payload.data)).then((un) => {
    if (cancelled) un();
    else stop = un;
  });
  return () => {
    cancelled = true;
    stop?.();
  };
}

async function shellCommand<T = void>(name: string, args?: Record<string, unknown>): Promise<T | undefined> {
  if (!inTauri) return mock.shell?.(name, args);
  return invoke<T>(name, args);
}

/** Await registration before requesting a snapshot, so a newer event cannot be missed. */
export async function onOverlay<T>(event: 'layout' | 'model' | 'action', handler: (data: T) => void): Promise<() => void> {
  if (!inTauri) return mock.onOverlay?.(`overlay://${event}`, handler) ?? (() => {});
  // Tauri's default Any target also receives emit_to events for other windows.
  // Layout and mirror messages must stay scoped to the calling capsule.
  return getCurrentWindow().listen<T>(`overlay://${event}`, (event) => handler(event.payload));
}

export const shell = {
  ready: () => shellCommand<any>('core_ready'),
  restartCore: () => shellCommand('core_restart'),
  overlayShow: () => shellCommand('overlay_show'),
  overlayHide: () => shellCommand('overlay_hide'),
  overlayInteractive: (rect: { x: number; y: number; width: number; height: number } | null) =>
    shellCommand('overlay_interactive', { rect }),
  overlayResetPosition: () => shellCommand('overlay_reset_position'),
  overlayMonitors: () => shellCommand<OverlayMonitor[]>('overlay_monitors'),
  overlayLayout: () => shellCommand<OverlayLayout>('overlay_layout'),
  overlayPublish: <T>(model: T) => shellCommand<OverlayEnvelope<T>>('overlay_publish', { model }),
  overlaySnapshot: <T>() => shellCommand<OverlayEnvelope<T> | null>('overlay_snapshot'),
  overlayAction: (action: unknown) => shellCommand('overlay_action', { action }),
  showMain: () => shellCommand('show_main_window'),
  hideMain: () => shellCommand('hide_main_window'),
  quit: () => shellCommand('quit_app'),
  /** 兼容管理员权限窗口: state, the switch (UAC + restart), and a restart into the task. */
  elevationStatus: () => shellCommand<ElevationStatus>('elevation_status'),
  elevationSet: (enabled: boolean) => shellCommand<{ restarting: boolean; warning?: string }>('elevation_set', { enabled }),
  elevationRestart: () => shellCommand<{ restarting: boolean }>('elevation_restart'),
};

export const isMock = !inTauri;

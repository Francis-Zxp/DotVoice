/* One controller interprets core events and owns timers. Mirrors only render its
   sequenced snapshot and send explicit user actions back to that controller. */
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { call, onCore, onOverlay, shell } from '../lib/bridge';
import { isAccurate } from '../lib/liveText';
import { pushLevel } from '../lib/store';
import type { OverlayEnvelope, OverlayLayout, Snapshot } from '../lib/types';
import { initial, reduce, type Model } from './machine';

interface Action { type: 'pin' | 'dismiss'; value?: boolean; session: number | null; token: number }

export function useOverlayModel(applyAppearance: (prefs: Snapshot['prefs']) => void) {
  const [layout, setLayout] = useState<OverlayLayout | null>(null);
  const [owned, dispatch] = useReducer(reduce, initial);
  const [mirrored, setMirrored] = useState(initial);
  const sequence = useRef(-1);
  const publishing = useRef(Promise.resolve());
  const controller = layout?.controller;

  useEffect(() => {
    let cancelled = false, received = false;
    let stop = () => {};
    const load = async () => {
      stop = await onOverlay<OverlayLayout>('layout', data => {
        received = true;
        if (!cancelled) setLayout(data);
      });
      if (cancelled) { stop(); return; }
      const current = await shell.overlayLayout();
      if (!cancelled && !received && current) setLayout(current);
    };
    void load().catch(console.error);
    return () => { cancelled = true; stop(); };
  }, []);

  useEffect(() => {
    if (controller == null) return;
    let cancelled = false;
    let stop = () => {};
    const load = async () => {
      if (controller) {
        stop = await onOverlay<Action>('action', action => {
          if (cancelled) return;
          const expected = { session: action.session, token: action.token };
          if (action.type === 'pin' && typeof action.value === 'boolean') dispatch({ type: 'pin', value: action.value, expected });
          else if (action.type === 'dismiss') dispatch({ type: 'dismiss', expected });
        });
      } else {
        const accept = (envelope: OverlayEnvelope<Model> | null | undefined) => {
          if (!cancelled && envelope && envelope.seq > sequence.current) {
            sequence.current = envelope.seq;
            setMirrored(envelope.model);
          }
        };
        stop = await onOverlay<OverlayEnvelope<Model>>('model', accept);
        if (!cancelled) accept(await shell.overlaySnapshot<Model>());
      }
      if (cancelled) stop();
    };
    void load().catch(console.error);
    return () => { cancelled = true; stop(); };
  }, [controller]);

  useEffect(() => {
    if (controller == null) return;
    let cancelled = false, tries = 0, retry = 0, receivedPrefs = false;
    const seen = { mode: false, state: false, tool: false, partial: false };
    const applyPrefs = (prefs: Snapshot['prefs']) => {
      applyAppearance(prefs);
      if (controller) dispatch({ type: 'config', enabled: prefs.overlay.enabled, onSwitch: prefs.overlay.on_switch, linger: prefs.overlay.linger_seconds });
      if (prefs.overlay.enabled) void shell.overlayShow(); else void shell.overlayHide();
    };
    const init = (s: Snapshot) => {
      applyPrefs(s.prefs);
      if (!controller) return;
      dispatch({ type: 'snapshot', snapshot: s, seen: { mode: false, state: false, tool: false, partial: false }, at: Date.now() });
    };
    const stop = onCore((event, data) => {
      if (cancelled) return;
      const at = Date.now();
      if (event === 'level') { pushLevel(data); return; }
      if (event === 'ready') { receivedPrefs = true; init(data); Object.assign(seen, { mode: true, state: true, tool: true, partial: true }); return; }
      if (event === 'prefs') { receivedPrefs = true; applyPrefs(data); return; }
      if (!controller) return;
      if (event === 'mode') seen.mode = true;
      if (event === 'tool' || event === 'mode' && data.tool) seen.tool = true;
      if (['core_exit', 'state', 'empty', 'session_failed', 'result'].includes(event)) seen.state = true;
      if (event === 'partial' && isAccurate(data.source)) seen.partial = true;
      if (event === 'mode') dispatch({ type: 'mode', value: data.value, pending: data.pending, armed: data.armed, source: data.source, tool: data.tool });
      else if (event === 'tool') dispatch({ type: 'tool', tool: data.tool });
      else if (event === 'core_exit') dispatch({ type: 'state', phase: 'disconnected', at });
      else if (event === 'state') dispatch({ type: 'state', phase: data.phase, armed: data.armed, message: data.message, at, destination: data.destination, whisper: typeof data.whisper === 'boolean' ? data.whisper : undefined });
      else if (event === 'drag') dispatch({ type: 'drag', locked: !!data.locked, how: data.how });
      else if (event === 'empty') dispatch({ type: 'empty' });
      else if (event === 'session_failed') dispatch({ type: 'failed', message: String(data?.message ?? '') });
      else if (event === 'wizard_result' && !data?.error) dispatch({ type: 'test' });
      else if (event === 'elevated') dispatch({ type: 'elevated' });
      else if (event === 'partial') dispatch({ type: 'partial', text: String(data.text ?? ''), source: data.source });
      else if (event === 'result') dispatch({ type: 'result', destination: data.destination, text: data.text, at: at / 1000, skipped: !!data.note_skipped,
        empty: !!data.empty_text, noteError: !!data.note_error, envelope: data.envelope, external: data.external ? String(data.app || '对方软件') : undefined, notTyped: !!data.not_typed, copied: !!data.copied });
    });
    const load = async () => {
      let snapshot: Snapshot | undefined;
      try { snapshot = await call<Snapshot>('snapshot'); } catch { snapshot = await shell.ready().catch(() => undefined); }
      if (cancelled) return;
      if (snapshot) {
        // Merge only fields for which no newer event arrived while this request was pending.
        if (!receivedPrefs) applyPrefs(snapshot.prefs);
        if (controller) dispatch({ type: 'snapshot', snapshot, seen: { ...seen }, at: Date.now() });
      } else if (++tries < 60) retry = window.setTimeout(load, 1000);
    };
    void load();
    return () => { cancelled = true; stop(); clearTimeout(retry); };
  }, [controller, applyAppearance]);

  useEffect(() => {
    if (!controller) return;
    publishing.current = publishing.current.then(async () => { await shell.overlayPublish(owned); }).catch(console.error);
  }, [controller, owned]);

  useEffect(() => {
    if (!controller || !owned.timer) return;
    const token = owned.token;
    const timer = window.setTimeout(() => dispatch({ type: 'timeout', token }), owned.timer.ms);
    return () => clearTimeout(timer);
  }, [controller, owned.token, owned.timer]);

  const model = controller ? owned : mirrored;
  const action = useCallback((type: Action['type']) => {
    const value = type === 'pin' ? !model.pinned : undefined;
    if (controller) dispatch(type === 'pin' ? { type, value } : { type });
    else void shell.overlayAction({ type, value, session: model.startedAt, token: model.token });
  }, [controller, model.pinned, model.startedAt, model.token]);
  return { model, layout, action };
}

/* One list of everything the user can pick up: each pen (one per receiver,
   several of the same model allowed) plus Tidepool. Pure, so it is tested. */
import type { Device, DeviceId, PenInstance, Phase, Tidepool, Usage } from './types';

export type RosterState = 'recording' | 'ready' | 'connected' | 'offline';

export interface RosterItem {
  key: string;
  kind: 'pen' | 'tidepool';
  model: DeviceId | 'tidepool';
  name: string;
  modelName: string;
  present: boolean;
  battery: number | null;
  batteryAt: number | null;
  state: RosterState;
  status: string;
  lastUsed: number | null;
  instanceId?: string;
}

export interface RosterInput {
  instances: PenInstance[];
  devices: Device[];
  selected: DeviceId;
  usage: Usage | null;
  tidepool: Tidepool;
  phase: Phase;
}

export const MODEL_NAME: Record<DeviceId | 'tidepool', string> = {
  baseus: '倍思 BS-OH085', pv60: '汉王 PV60', tidepool: 'Tidepool OS',
};

const STATUS: Record<RosterState, string> = {
  recording: '正在录音', ready: '待命', connected: '已连接', offline: '未连接',
};

export const TIDEPOOL_BATTERY_NOTE = 'Tidepool 固件 4.0.6 起才把电量发给电脑；电量变化时随心跳捎带，不额外耗电';

const armedPhases: Phase[] = ['idle', 'starting', 'recording', 'processing'];

/** `includeAbsent`: also list pens that were seen before but are not plugged in now
 *  (and Tidepool when its receiver is installed but the device is away). */
export function roster(input: RosterInput, { includeAbsent = false } = {}): RosterItem[] {
  const { instances, devices, selected, usage, tidepool, phase } = input;
  const armed = armedPhases.includes(phase);
  const items: RosterItem[] = [];
  if (instances.length) {
    for (const pen of instances) {
      if (!pen.present && !includeAbsent) continue;
      const state: RosterState = pen.active ? 'recording' : !pen.present ? 'offline' : armed ? 'ready' : 'connected';
      items.push({
        key: pen.id, kind: 'pen', model: pen.model, name: pen.label?.trim() || MODEL_NAME[pen.model],
        modelName: MODEL_NAME[pen.model], present: pen.present,
        battery: pen.battery?.percent ?? null, batteryAt: pen.battery?.reported_at ?? null,
        state, status: STATUS[state], lastUsed: pen.last_used ?? null, instanceId: pen.id,
      });
    }
  } else {
    // An older core only reports models; the ledger battery belongs to the selected Baseus.
    for (const device of devices) {
      if (!device.present && !includeAbsent) continue;
      const battery = device.id === 'baseus' && selected === 'baseus' ? usage?.battery ?? null : null;
      const state: RosterState = !device.present ? 'offline'
        : device.id === selected && phase === 'recording' ? 'recording' : armed && device.id === selected ? 'ready' : 'connected';
      items.push({
        key: device.id, kind: 'pen', model: device.id, name: device.name, modelName: device.name, present: device.present,
        battery: battery?.percent ?? null, batteryAt: battery?.reported_at ?? null,
        state, status: STATUS[state], lastUsed: null,
      });
    }
  }
  const online = !!(tidepool.device_online || tidepool.usb_connected);
  if (online || (includeAbsent && tidepool.installed)) {
    const state: RosterState = tidepool.recording ? 'recording' : online ? 'ready' : 'offline';
    items.push({
      key: 'tidepool', kind: 'tidepool', model: 'tidepool', name: 'Tidepool', modelName: MODEL_NAME.tidepool, present: online,
      battery: tidepool.battery_percent ?? null, batteryAt: tidepool.battery_reported_at ?? null, state,
      status: tidepool.recording ? '正在录音' : tidepool.usb_connected ? 'USB 已连接' : tidepool.device_online ? '无线在线' : '未连接',
      lastUsed: null,
    });
  }
  return items;
}

export type BatteryTone = 'none' | 'low' | 'mid' | 'ok';
export function batteryTone(percent: number | null | undefined): BatteryTone {
  if (percent == null) return 'none';
  return percent <= 20 ? 'low' : percent <= 40 ? 'mid' : 'ok';
}

/** Labels are short, single-line and never empty after trimming. */
export function cleanLabel(value: string, max = 16) {
  return Array.from(value.replace(/\s+/g, ' ').trim()).slice(0, max).join('');
}

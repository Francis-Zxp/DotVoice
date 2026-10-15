import { describe, expect, it } from 'vitest';
import { batteryTone, cleanLabel, roster, type RosterInput } from './devices';
import type { PenInstance } from './types';

const pen = (over: Partial<PenInstance>): PenInstance => ({
  id: 'baseus:1', model: 'baseus', label: '倍思 #1', present: true,
  battery: { percent: 76, reported_at: 100 }, last_used: 50, active: false, ...over,
});

const base: RosterInput = {
  instances: [], selected: 'baseus', usage: null, tidepool: {}, phase: 'idle',
  devices: [
    { id: 'baseus', name: '倍思 BS-OH085', short: 'BS-OH085', present: true, validation: '', modes: [] },
    { id: 'pv60', name: '汉王 PV60', short: 'PV60', present: false, validation: '', modes: [] },
  ],
};

describe('roster', () => {
  it('lists every present pen with its own battery, plus Tidepool when online', () => {
    const items = roster({
      ...base,
      instances: [pen({}), pen({ id: 'baseus:2', label: '倍思 #2', battery: { percent: 18, reported_at: 90 } }),
        pen({ id: 'baseus:3', label: '倍思 #3', present: false })],
      tidepool: { device_online: true, battery_percent: null },
    });
    expect(items.map((i) => i.name)).toEqual(['倍思 #1', '倍思 #2', 'Tidepool']);
    expect(items.map((i) => i.battery)).toEqual([76, 18, null]);
    expect(items[2].status).toBe('无线在线');
  });

  it('includes absent pens and an installed-but-away Tidepool on request', () => {
    const items = roster({ ...base, instances: [pen({ present: false })], tidepool: { installed: true, device_online: false } }, { includeAbsent: true });
    expect(items.map((i) => [i.name, i.state])).toEqual([['倍思 #1', 'offline'], ['Tidepool', 'offline']]);
  });

  it('marks the pen that is recording, and ready / connected by the armed state', () => {
    const instances = [pen({ active: true }), pen({ id: 'baseus:2', label: '倍思 #2' })];
    expect(roster({ ...base, instances, phase: 'recording' }).map((i) => i.status)).toEqual(['正在录音', '待命']);
    expect(roster({ ...base, instances: [pen({})], phase: 'disconnected' })[0].status).toBe('已连接');
  });

  it('falls back to the model list of an older core, battery only for the selected Baseus', () => {
    const usage = { battery: { percent: 64, reported_at: 10, source: 'baseus_hid' } } as RosterInput['usage'];
    const items = roster({ ...base, usage });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ name: '倍思 BS-OH085', battery: 64, state: 'ready' });
  });

  it('uses the model name when a label is blank', () => {
    expect(roster({ ...base, instances: [pen({ label: '  ' })] })[0].name).toBe('倍思 BS-OH085');
  });
});

describe('helpers', () => {
  it('grades battery levels', () => {
    expect([null, 12, 20, 33, 80].map(batteryTone)).toEqual(['none', 'low', 'low', 'mid', 'ok']);
  });
  it('cleans labels to one short line', () => {
    expect(cleanLabel('  会议室\n 笔  ')).toBe('会议室 笔');
    expect(cleanLabel('一二三四五六七八九十一二三四五六七八')).toHaveLength(16);
  });
});

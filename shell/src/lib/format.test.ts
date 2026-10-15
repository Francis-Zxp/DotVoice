import { describe, expect, it } from 'vitest';
import { when } from './format';

const at = (y: number, m: number, d: number, h = 9, min = 5) => new Date(y, m - 1, d, h, min).getTime();

describe('when', () => {
  const now = at(2026, 10, 8, 12, 0);
  it('names today and yesterday with the time', () => {
    expect(when(at(2026, 10, 8, 8, 3) / 1000, now)).toBe('今天 08:03');
    expect(when(at(2026, 10, 7, 23, 12) / 1000, now)).toBe('昨天 23:12');
  });
  it('uses month and day within the year, the full date before it', () => {
    expect(when(at(2026, 10, 5, 23, 19) / 1000, now)).toBe('10月5日 23:19');
    expect(when(at(2025, 3, 1) / 1000, now)).toBe('2025年3月1日');
  });
});

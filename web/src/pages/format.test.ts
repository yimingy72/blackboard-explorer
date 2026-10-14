import { expect, test } from 'vitest';
import { formatMoney, formatTime } from './format';

test('money preserves recorded currency and never relabels unknown or USD as yuan', () => {
  expect(formatMoney('10.04', 'CNY')).toBe('¥10.04');
  expect(formatMoney('10.04', 'USD')).toBe('US$10.04');
  expect(formatMoney('0.0001', 'CNY')).toBe('¥<0.001');
  expect(formatMoney('1', null)).toBe('币种未配置 1.00');
});

test('conversation time uses a consistent 24-hour clock and handles invalid records', () => {
  expect(formatTime('2026-10-11T16:30:00')).toBe('16:30:00');
  expect(formatTime('not-a-date')).toBe('—');
});

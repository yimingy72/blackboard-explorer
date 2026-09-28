import { expect, test } from 'vitest';
import { formatMoney } from './format';

test('money preserves recorded currency and never relabels unknown or USD as yuan', () => {
  expect(formatMoney('10.04', 'CNY')).toBe('¥10.04');
  expect(formatMoney('10.04', 'USD')).toBe('US$10.04');
  expect(formatMoney('0.0001', 'CNY')).toBe('¥<0.001');
  expect(formatMoney('1', null)).toBe('币种未配置 1.00');
});

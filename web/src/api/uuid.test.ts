import { afterEach, expect, it, vi } from 'vitest';
import { randomUUID } from './uuid';

afterEach(() => vi.unstubAllGlobals());

it('uses the native UUID generator when available', () => {
  const native = vi.fn(() => '10000000-0000-4000-8000-000000000001');
  vi.stubGlobal('crypto', { randomUUID: native });
  expect(randomUUID()).toBe('10000000-0000-4000-8000-000000000001');
  expect(native).toHaveBeenCalledOnce();
});

it('generates distinct UUID v4 request IDs without the secure-context API', () => {
  const webCrypto = globalThis.crypto;
  vi.stubGlobal('crypto', { getRandomValues: webCrypto.getRandomValues.bind(webCrypto) });
  const ids = Array.from({ length: 100 }, randomUUID);
  expect(ids.every(id => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id))).toBe(true);
  expect(new Set(ids).size).toBe(ids.length);
});

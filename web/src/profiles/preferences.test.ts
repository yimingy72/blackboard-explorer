import { afterEach, describe, expect, it, vi } from 'vitest';
import { readDefaultProfile, saveDefaultProfile } from './preferences';

const values = new Map<string, string>();

afterEach(() => {
  values.clear();
  vi.unstubAllGlobals();
});

function browserStorage() {
  vi.stubGlobal('window', {
    localStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    },
  });
}

describe('browser profile default', () => {
  it('stores a specific name and immutable version', () => {
    browserStorage();
    expect(readDefaultProfile()).toBeNull();
    saveDefaultProfile('team', 3);
    expect(readDefaultProfile()).toEqual({ name: 'team', version: 3 });
  });

  it('ignores malformed saved values and rejects invalid writes', () => {
    browserStorage();
    values.set('bbx.default-profile.v1', '{');
    expect(readDefaultProfile()).toBeNull();
    values.set('bbx.default-profile.v1', '{"name":"team","version":0}');
    expect(readDefaultProfile()).toBeNull();
    expect(() => saveDefaultProfile('', 1)).toThrow();
    expect(() => saveDefaultProfile('team', 1.5)).toThrow();
  });
});

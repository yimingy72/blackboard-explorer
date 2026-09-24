/** A creation default for this browser only; existing tasks keep their pinned version. */
export type DefaultProfile = { name: string; version: number };

const STORAGE_KEY = 'bbx.default-profile.v1';

export function readDefaultProfile(): DefaultProfile | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return null;
    const profile = value as Record<string, unknown>;
    if (typeof profile.name !== 'string' || !profile.name.trim()) return null;
    if (typeof profile.version !== 'number' || !Number.isSafeInteger(profile.version) || profile.version < 1) return null;
    return { name: profile.name, version: profile.version };
  } catch {
    return null;
  }
}

export function saveDefaultProfile(name: string, version: number): void {
  if (!name.trim() || !Number.isSafeInteger(version) || version < 1) {
    throw new Error('配置名称与版本必须有效。');
  }
  if (typeof window === 'undefined') throw new Error('当前环境无法保存浏览器默认配置。');
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ name: name.trim(), version }));
}

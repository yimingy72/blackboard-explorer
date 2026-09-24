import YAML from 'yaml';
import type { components } from '../api/schema';

export type ProfileInput = components['schemas']['AgentProfile-Input'];
export type DiffLine = { kind: 'equal' | 'added' | 'removed'; text: string };

const ROLES = ['explore', 'derive', 'close'] as const;
const FIELDS = new Set([
  'models', 'params', 'prompts', 'prompt_templates', 'exec_image',
  'exec_resources', 'privileged_allowlist',
]);

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} 必须是对象。`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): void {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${label} 必须填写文本。`);
  }
}

/** Early shape feedback only. The blackboard's AgentProfile remains authoritative. */
export function parseProfileYaml(source: string): ProfileInput {
  const document = YAML.parseDocument(source, { uniqueKeys: true });
  if (document.errors.length) throw new Error(`YAML 格式错误：${document.errors[0].message}`);
  let value: unknown;
  try {
    value = document.toJS({ maxAliasCount: 100 });
  } catch (error) {
    throw new Error(`YAML 内容无法解析：${error instanceof Error ? error.message : '未知错误'}`);
  }
  const profile = record(value, '配置');
  for (const key of Object.keys(profile)) {
    if (!FIELDS.has(key)) throw new Error(`未知配置字段：${key}。`);
  }
  const models = record(profile.models, 'models');
  const prompts = record(profile.prompts, 'prompts');
  const templates = record(profile.prompt_templates, 'prompt_templates');
  for (const role of ROLES) {
    const model = record(models[role], `models.${role}`);
    for (const key of ['provider', 'model', 'base_url', 'reasoning_effort']) {
      text(model[key], `models.${role}.${key}`);
    }
    record(model.price, `models.${role}.price`);
    text(prompts[role], `prompts.${role}`);
    text(templates[role], `prompt_templates.${role}`);
  }
  record(profile.params, 'params');
  text(profile.exec_image, 'exec_image');
  const resources = record(profile.exec_resources, 'exec_resources');
  if (typeof resources.cpus !== 'number' || resources.cpus <= 0) {
    throw new Error('exec_resources.cpus 必须是正数。');
  }
  text(resources.mem, 'exec_resources.mem');
  if (typeof resources.pids !== 'number' || !Number.isInteger(resources.pids) || resources.pids < 1) {
    throw new Error('exec_resources.pids 必须是正整数。');
  }
  if (!Array.isArray(profile.privileged_allowlist) || profile.privileged_allowlist.some((item) => typeof item !== 'string')) {
    throw new Error('privileged_allowlist 必须是字符串列表。');
  }
  return profile as ProfileInput;
}

export function stringifyProfileYaml(profile: ProfileInput): string {
  return YAML.stringify(profile, { lineWidth: 0 });
}

function lines(value: string): string[] {
  const normalized = value.replace(/\r\n/g, '\n').replace(/\n$/, '');
  return normalized ? normalized.split('\n') : [];
}

/** Line diff, preserving unchanged context; very large inputs fall back to whole blocks. */
export function diffYaml(left: string, right: string): DiffLine[] {
  const before = lines(left);
  const after = lines(right);
  if (before.length * after.length > 2_000_000) {
    return [
      ...before.map((text): DiffLine => ({ kind: 'removed', text })),
      ...after.map((text): DiffLine => ({ kind: 'added', text })),
    ];
  }
  const common = Array.from({ length: before.length + 1 }, () => new Uint32Array(after.length + 1));
  for (let i = before.length - 1; i >= 0; i--) {
    for (let j = after.length - 1; j >= 0; j--) {
      common[i][j] = before[i] === after[j] ? common[i + 1][j + 1] + 1 : Math.max(common[i + 1][j], common[i][j + 1]);
    }
  }
  const result: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < before.length || j < after.length) {
    if (i < before.length && j < after.length && before[i] === after[j]) {
      result.push({ kind: 'equal', text: before[i++] });
      j++;
    } else if (j < after.length && (i === before.length || common[i][j + 1] > common[i + 1][j])) {
      result.push({ kind: 'added', text: after[j++] });
    } else {
      result.push({ kind: 'removed', text: before[i++] });
    }
  }
  return result;
}

export function diffProfileVersions(left: ProfileInput, right: ProfileInput): DiffLine[] {
  return diffYaml(stringifyProfileYaml(left), stringifyProfileYaml(right));
}

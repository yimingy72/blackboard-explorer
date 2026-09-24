import { describe, expect, it } from 'vitest';
import YAML from 'yaml';
import type { ProfileInput } from './yaml';
import { diffProfileVersions, diffYaml, parseProfileYaml, stringifyProfileYaml } from './yaml';

const model = {
  provider: 'deepseek', model: 'deepseek-flash', base_url: 'https://api.deepseek.com',
  reasoning_effort: 'high',
  price: { currency: 'USD', cache_hit_per_m: '0.01', cache_miss_per_m: '0.3', output_per_m: '1.2', off_peak: false },
};

const profile: ProfileInput = {
  models: { explore: model, derive: model, close: model },
  params: {
    explore_max_steps: 60, seed_max_steps: 20, context_threshold: 128000,
    conclude_grace_calls: 3, grace_timeout: 5, heartbeat_timeout: 30,
    intent_max_attempts: 3, max_consecutive_failures: 3, derive_empty_limit: 2,
    close_reserve_ratio: '0.05', snapshot_max_lines: 150, delta_max_lines: 15,
    dispute_notify_depth: 2,
  },
  prompts: { explore: 'prompts/explore.md.j2', derive: 'prompts/derive.md.j2', close: 'prompts/close.md.j2' },
  prompt_templates: { explore: '第一行\n第二行', derive: '推导说明', close: '裁定说明' },
  exec_image: 'bbx-exec-env:latest',
  exec_resources: { cpus: 2, mem: '4g', pids: 256 },
  privileged_allowlist: [],
};

describe('full profile YAML', () => {
  it('round-trips the complete profile including prompt bodies', () => {
    const parsed = parseProfileYaml(stringifyProfileYaml(profile));
    expect(parsed).toEqual(profile);
    expect(parsed.prompt_templates.explore).toBe('第一行\n第二行');
  });

  it('rejects syntax, duplicate keys, and missing required bodies before publishing', () => {
    expect(() => parseProfileYaml('models: [')).toThrow('YAML 格式错误');
    expect(() => parseProfileYaml('models: {}\nmodels: {}')).toThrow('YAML 格式错误');
    const noTemplates = { ...profile, prompt_templates: undefined };
    expect(() => parseProfileYaml(YAML.stringify(noTemplates))).toThrow('prompt_templates');
  });

  it('keeps unchanged context and shows old/new lines in a version diff', () => {
    expect(diffYaml('a\nb\nc\n', 'a\nB\nc\n')).toEqual([
      { kind: 'equal', text: 'a' },
      { kind: 'removed', text: 'b' },
      { kind: 'added', text: 'B' },
      { kind: 'equal', text: 'c' },
    ]);
    const edited: ProfileInput = {
      ...profile,
      prompt_templates: { ...profile.prompt_templates, derive: '新的推导说明' },
    };
    const diff = diffProfileVersions(profile, edited);
    expect(diff.some((line) => line.kind === 'removed' && line.text.includes('推导说明'))).toBe(true);
    expect(diff.some((line) => line.kind === 'added' && line.text.includes('新的推导说明'))).toBe(true);
    expect(diffProfileVersions(profile, profile).every((line) => line.kind === 'equal')).toBe(true);
  });
});

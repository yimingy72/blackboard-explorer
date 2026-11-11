import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';
import { mergeTextareaEdit, promptSections } from './promptSections';

it('preserves prefix, boundaries, neighboring Jinja, and mixed line endings while editing one section', () => {
  const source = '前言\r\n\r\n# 角色\r\n\r\n正文\r\n\r\n# 工作\r\n{% if goal %}\n# 嵌套\n{{ goal }}\n{% endif %}\r\n';
  const parts = promptSections(source);
  expect(parts.map(part => part.label)).toEqual(['开头', '角色', '工作']);
  const { start, end } = parts[1];
  const body = mergeTextareaEdit(source.slice(start, end), '\n新正文');
  expect(source.slice(0, start) + body + source.slice(end)).toBe(source.replace('正文', '新正文'));
});

it('falls back for unknown template delimiters and leaves unchanged textarea text exact', () => {
  expect(promptSections('# 角色\n{% raw %}\n# 样例\n{% endraw %}\n# 交付')).toEqual([]);
  const original = '自定义\r\n{{ goal }}\n尾部\r\n';
  expect(promptSections(original)).toEqual([]);
  expect(mergeTextareaEdit(original, '自定义\n{{ goal }}\n尾部\n')).toBe(original);
  expect(mergeTextareaEdit(original, '自定义\n{{ goal }}\n末尾\n')).toBe('自定义\r\n{{ goal }}\n末尾\r\n');
});

it.each(['explore', 'derive', 'close'])('recognizes real %s template sections', role => {
  const file = new URL('../../../profiles/default/prompts/' + role + '.md.j2', import.meta.url);
  expect(promptSections(readFileSync(file, 'utf8')).length).toBeGreaterThanOrEqual(4);
});

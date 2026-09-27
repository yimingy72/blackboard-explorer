import { expect, it } from 'vitest';
import { readableProse } from './readable';

it('lays out inline numbered prose while preserving code and links', () => {
  const inlineCode = { type: 'inlineCode', value: 'call(1); call(2)' };
  const link = { type: 'link', url: 'https://example.test/(1)/(2)', children: [{ type: 'text', value: 'source' }] };
  const tree = { type: 'root', children: [{ type: 'paragraph', children: [
    { type: 'text', value: '共享资源：（1）运行 ' }, inlineCode,
    { type: 'text', value: '；（2）核对 ' }, link,
  ] }] };
  readableProse()(tree);
  expect(tree.children.map((node) => node.type)).toEqual(['paragraph', 'list']);
  expect(JSON.stringify(tree)).toContain('call(1); call(2)');
  expect(JSON.stringify(tree)).toContain('https://example.test/(1)/(2)');
});

it('leaves code blocks and nonsequential parenthesized values unchanged', () => {
  const tree = { type: 'root', children: [
    { type: 'code', value: '(1) value\n(2) value' },
    { type: 'paragraph', children: [{ type: 'text', value: '返回 (2) 条记录，另外 (7) 个文件。' }] },
  ] };
  const before = JSON.stringify(tree);
  readableProse()(tree);
  expect(JSON.stringify(tree)).toBe(before);
});

it('breaks long prose into paragraphs without changing sentence text', () => {
  const content = '这是一段完整的观察说明，需要保持所有文字与标点不变。'.repeat(16);
  const tree = { type: 'root', children: [{ type: 'paragraph', children: [{ type: 'text', value: content }] }] };
  readableProse()(tree);
  expect(tree.children.length).toBeGreaterThan(1);
  expect(tree.children.flatMap((node) => node.children).map((node) => node.value).join('')).toBe(content);
});

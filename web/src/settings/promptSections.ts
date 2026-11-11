export type PromptSection = { label: string; start: number; end: number };

const paired = new Set(['if', 'for', 'block', 'macro', 'filter', 'with', 'call', 'autoescape']);
const simple = new Set(['elif', 'else', 'include', 'import', 'from', 'extends', 'do']);

// The original string is authoritative. A heading inside a template block or code
// fence cannot be a boundary; unfamiliar template syntax uses the full editor.
export function promptSections(source: string): PromptSection[] {
  const tags = /\{%[-+]?\s*(\w+)([^\r\n]*?)[-+]?%\}/g;
  const heads: { label: string; level: number; offset: number; body: number }[] = [];
  const stack: string[] = [];
  let fence = '';
  for (const line of source.matchAll(/[^\r\n]*(?:\r\n|\r|\n|$)/g)) {
    const raw = line[0], offset = line.index;
    if (!raw) continue;
    const text = raw.replace(/[\r\n]+$/, '');
    const mark = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text);
    const inFence = Boolean(fence || mark);
    if (mark) {
      if (!fence) fence = mark[1];
      else if (mark[1][0] === fence[0] && mark[1].length >= fence.length && !mark[2].trim()) fence = '';
    }
    if (text.includes('{#') || text.includes('#}')) return [];
    const substitutions = text.match(/\{\{[^{}]*\}\}/g) ?? [];
    if ((text.match(/\{\{/g)?.length ?? 0) !== substitutions.length) return [];
    const directives = [...text.matchAll(tags)];
    if ((text.match(/\{%|%\}/g)?.length ?? 0) !== directives.length * 2) return [];
    if (directives.length) {
      if (inFence || directives.length !== 1 || text.trim() !== directives[0][0]) return [];
      const [, name, args] = directives[0];
      if (paired.has(name)) stack.push(name);
      else if (name.startsWith('end')) {
        if (stack.pop() !== name.slice(3)) return [];
      } else if (!simple.has(name) && !(name === 'set' && args.includes('='))) return [];
    }
    if (stack.length || inFence || directives.length) continue;
    const heading = /^(#{1,2})[ \t]+([^#{}<>]+?)[ \t]*$/.exec(text);
    if (heading) heads.push({ label: heading[2], level: heading[1].length, offset, body: offset + raw.length });
  }
  if (stack.length || fence || heads.length < 2) return [];
  const level = Math.min(...heads.map(h => h.level));
  const top = heads.filter(h => h.level === level);
  if (top.length < 2 || top.length > 14 || new Set(top.map(h => h.label)).size !== top.length) return [];
  // The separator immediately before the next heading stays outside editable content.
  const ranges = top.map((head, i) => {
    const boundary = top[i + 1]?.offset ?? source.length;
    const body = source.slice(head.body, boundary);
    return { label: head.label, start: head.body, end: boundary - (body.match(/(?:\r\n|\n|\r)+$/)?.[0].length ?? 0) };
  });
  if (ranges.slice(0, -1).some((range, i) => range.end === top[i + 1].offset)) return [];
  if (source.slice(0, top[0].offset).trim()) {
    const end = top[0].offset;
    ranges.unshift({ label: '开头', start: 0, end: end - (source.slice(0, end).match(/(?:\r\n|\r|\n)+$/)?.[0].length ?? 0) });
  }
  return ranges;
}

// Browsers normalize textarea line endings. Only replace the changed range so
// existing mixed line endings outside that range stay byte for byte.
export function mergeTextareaEdit(raw: string, next: string) {
  const normalized = raw.replace(/\r\n?|\n/g, '\n');
  if (normalized === next) return raw;
  let start = 0, beforeEnd = normalized.length, afterEnd = next.length;
  while (start < beforeEnd && start < afterEnd && normalized[start] === next[start]) start++;
  while (beforeEnd > start && afterEnd > start && normalized[beforeEnd - 1] === next[afterEnd - 1]) {
    beforeEnd--; afterEnd--;
  }
  let index = 0, rawStart = 0, rawEnd = 0;
  for (let i = 0; i <= raw.length; i++, index++) {
    if (index === start) rawStart = i;
    if (index === beforeEnd) { rawEnd = i; break; }
    if (raw[i] === '\r' && raw[i + 1] === '\n') i++;
  }
  const eol = raw.match(/\r\n|\r|\n/)?.[0] ?? '\n';
  return raw.slice(0, rawStart) + next.slice(start, afterEnd).replace(/\n/g, eol) + raw.slice(rawEnd);
}

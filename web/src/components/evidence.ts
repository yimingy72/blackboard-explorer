export type EvidenceRecord = {
  type: string;
  summary: string;
  uri: string | null;
  path: string | null;
  callId: string | null;
  size: number | null;
  auto: boolean;
};

export type TextPart = { text: string; match: boolean };

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function parseEvidence(value: unknown): EvidenceRecord | null {
  if (!isRecord(value) || typeof value.type !== 'string') return null;
  return {
    type: value.type,
    summary: typeof value.summary === 'string' ? value.summary : '',
    uri: typeof value.uri === 'string' ? value.uri : null,
    path: typeof value.path === 'string' ? value.path : null,
    callId: typeof value.call_id === 'string' ? value.call_id : null,
    size: typeof value.size === 'number' && value.size >= 0 ? value.size : null,
    auto: value.auto === true,
  };
}

function parseJson(value: string): unknown | null {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return null;
  }
}

function display(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
}

export function parseHttpPreview(raw: string): { request: string; response: string } {
  const parsed = parseJson(raw);
  if (isRecord(parsed) && 'request' in parsed && 'response' in parsed) {
    return { request: display(parsed.request), response: display(parsed.response) };
  }
  const match = /(?:^|\r?\n)(HTTP\/\d(?:\.\d)?\s+\d{3}\b)/m.exec(raw);
  if (!match || match.index === undefined) return { request: raw, response: '' };
  const offset = match.index + (match[0].startsWith('\r\n') ? 2 : match[0].startsWith('\n') ? 1 : 0);
  return { request: raw.slice(0, offset).trimEnd(), response: raw.slice(offset).trimStart() };
}

export function parseScriptPreview(raw: string): { script: string; output: string } {
  const parsed = parseJson(raw);
  if (isRecord(parsed) && 'script' in parsed && 'output' in parsed) {
    return { script: display(parsed.script), output: display(parsed.output) };
  }
  const marker = /^[-=]{2,}\s*(?:output|输出)\s*[-=]{2,}\s*$/im.exec(raw);
  if (!marker || marker.index === undefined) return { script: raw, output: '' };
  return {
    script: raw.slice(0, marker.index).trimEnd(),
    output: raw.slice(marker.index + marker[0].length).trimStart(),
  };
}

export function flattenToolResult(value: unknown, depth = 0): string {
  if (depth > 4) return display(value);
  if (typeof value === 'string') {
    const trimmed = value.trim();
    const parsed = trimmed.startsWith('[') || trimmed.startsWith('{') ? parseJson(trimmed) : null;
    return parsed === null ? value : flattenToolResult(parsed, depth + 1);
  }
  if (Array.isArray(value)) {
    return value.map((item) => flattenToolResult(item, depth + 1)).join('\n');
  }
  if (isRecord(value)) {
    if (value.type === 'text' && typeof value.text === 'string') {
      return flattenToolResult(value.text, depth + 1);
    }
    if (value.type === 'function_result' && 'result' in value) {
      return flattenToolResult(value.result, depth + 1);
    }
    if ('stdout' in value || 'stderr' in value) {
      const lines = [];
      if ('exit_code' in value) lines.push(`退出码：${display(value.exit_code)}`);
      if ('stdout' in value) lines.push(`标准输出\n${display(value.stdout)}`);
      if ('stderr' in value && value.stderr) lines.push(`标准错误\n${display(value.stderr)}`);
      return lines.join('\n\n');
    }
  }
  return display(value);
}

export function parseToolLog(raw: string): { tool: string; args: string; result: string } | null {
  const parsed = parseJson(raw);
  if (!isRecord(parsed) || typeof parsed.tool !== 'string' || !('result' in parsed)) return null;
  return {
    tool: parsed.tool,
    args: display(parsed.args ?? {}),
    result: flattenToolResult(parsed.result),
  };
}

export function logKeywords(summary: string): string[] {
  const quoted = [...summary.matchAll(/['"`“”‘’]([^'"`“”‘’]{2,80})['"`“”‘’]/g)]
    .map((match) => match[1].trim())
    .filter(Boolean);
  if (quoted.length) return quoted.slice(0, 5);
  return (summary.match(/[\p{L}\p{N}_]+/gu) ?? [])
    .filter((word) => word.length >= 3 && word.length <= 80)
    .slice(0, 5);
}

export function highlightLiteral(raw: string, keywords: string[]): TextPart[] {
  const terms = keywords.filter(Boolean);
  if (!terms.length) return [{ text: raw, match: false }];
  const lower = raw.toLocaleLowerCase();
  const parts: TextPart[] = [];
  let cursor = 0;
  while (cursor < raw.length) {
    let found = -1;
    let length = 0;
    for (const term of terms) {
      const index = lower.indexOf(term.toLocaleLowerCase(), cursor);
      if (index >= 0 && (found < 0 || index < found)) {
        found = index;
        length = term.length;
      }
    }
    if (found < 0) {
      parts.push({ text: raw.slice(cursor), match: false });
      break;
    }
    if (found > cursor) parts.push({ text: raw.slice(cursor, found), match: false });
    parts.push({ text: raw.slice(found, found + length), match: true });
    cursor = found + length;
  }
  return parts;
}

export function codeLines(raw: string, path: string | null): string {
  const start = Number(/:(\d+)(?:-\d+)?$/.exec(path ?? '')?.[1] ?? 1);
  return raw.split(/\r?\n/).map((line, index) => {
    const numbered = /^\s*(\d+)\s*[|:]\s?(.*)$/.exec(line);
    const number = numbered ? Number(numbered[1]) : start + index;
    return `${String(number).padStart(4, ' ')} │ ${numbered ? numbered[2] : line}`;
  }).join('\n');
}

export function formatBytes(value: number): string {
  return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

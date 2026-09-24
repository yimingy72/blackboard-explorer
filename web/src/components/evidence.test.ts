import { describe, expect, it } from 'vitest';
import {
  codeLines,
  flattenToolResult,
  highlightLiteral,
  logKeywords,
  parseEvidence,
  parseHttpPreview,
  parseScriptPreview,
  parseToolLog,
} from './evidence';

describe('evidence parsing', () => {
  it('accepts only record-shaped evidence and keeps optional fields bounded', () => {
    expect(parseEvidence(null)).toBeNull();
    expect(parseEvidence({ type: 'log', summary: '关键字 "timeout"', uri: 'evidence/x', size: 12 })).toEqual({
      type: 'log', summary: '关键字 "timeout"', uri: 'evidence/x', size: 12,
      path: null, callId: null, auto: false,
    });
    expect(parseEvidence({ type: 'text', size: -1 })?.size).toBeNull();
  });

  it('splits HTTP and script material without treating content as markup', () => {
    expect(parseHttpPreview('GET /orders HTTP/1.1\nHost: app\n\nHTTP/1.1 502 Bad Gateway\nfail')).toEqual({
      request: 'GET /orders HTTP/1.1\nHost: app', response: 'HTTP/1.1 502 Bad Gateway\nfail',
    });
    expect(parseHttpPreview('{"request":"POST /", "response":{"status":403}}').response).toContain('403');
    expect(parseScriptPreview('print(1)\n--- output ---\n1')).toEqual({ script: 'print(1)', output: '1' });
    expect(parseScriptPreview('plain script')).toEqual({ script: 'plain script', output: '' });
  });

  it('extracts ToolLog JSON including nested MAF Content arrays', () => {
    const nested = JSON.stringify([{ type: 'text', text: JSON.stringify({ exit_code: 0, stdout: '<command_output>ok</command_output>', stderr: '' }) }]);
    const parsed = parseToolLog(JSON.stringify({ tool: 'execute_command', args: { command: 'echo ok' }, result: nested }));
    expect(parsed?.tool).toBe('execute_command');
    expect(parsed?.args).toContain('echo ok');
    expect(parsed?.result).toContain('标准输出');
    expect(parsed?.result).toContain('<command_output>ok</command_output>');
    expect(flattenToolResult([{ type: 'text', text: '<b>literal</b>' }])).toBe('<b>literal</b>');
    expect(parseToolLog('not JSON')).toBeNull();
  });

  it('highlights literal keywords and numbers code references', () => {
    const terms = logKeywords('日志关键字 "Pool Exhausted"，请复查');
    expect(terms).toEqual(['Pool Exhausted']);
    expect(highlightLiteral('POOL EXHAUSTED\nnormal', terms)).toEqual([
      { text: 'POOL EXHAUSTED', match: true }, { text: '\nnormal', match: false },
    ]);
    expect(highlightLiteral('a+b', ['a+b'])).toEqual([{ text: 'a+b', match: true }]);
    expect(codeLines('first\nsecond', 'gateway.py:41')).toBe('  41 │ first\n  42 │ second');
    expect(codeLines('20: existing line', null)).toBe('  20 │ existing line');
  });
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import { trafficApi } from './traffic';

afterEach(() => vi.unstubAllGlobals());

describe('traffic requests', () => {
  it('keeps task, run and Agent filters separate from search parameters', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const abort = new AbortController();
    await trafficApi.list('task-1', { run: 2, agentId: 'member-1', protocol: 'udp', query: '/?agent_id=other&run=9', before: 231 }, abort.signal);
    const [path, init] = fetch.mock.calls[0];
    const url = new URL(path, 'http://example.invalid');
    expect(url.pathname).toBe('/api/tasks/task-1/traffic');
    expect(url.searchParams.get('run')).toBe('2');
    expect(url.searchParams.get('agent_id')).toBe('member-1');
    expect(url.searchParams.get('q')).toBe('/?agent_id=other&run=9');
    expect(url.searchParams.get('before')).toBe('231');
    expect(init.signal).toBe(abort.signal);
    expect(init.credentials).toBe('include');
  });

  it('requests archived record details in the selected run', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await trafficApi.detail('task-2', 1, 'r-35');
    expect(fetch.mock.calls[0][0]).toBe('/api/tasks/task-2/traffic/records/r-35?run=1');
    expect(trafficApi.download('task-2', 1, 'source-1')).toBe('/api/tasks/task-2/traffic/files/source-1?run=1');
  });

  it('uses an ascending packet cursor independently from Agent, direction and search', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await trafficApi.list('task-1', { run: 3, kind: 'packet', after: 0, direction: 'outbound', agentId: 'lead', limit: 200 });
    const url = new URL(fetch.mock.calls[0][0], 'http://example.invalid');
    expect(Object.fromEntries(url.searchParams)).toEqual({ run: '3', limit: '200', kind: 'packet', after: '0', direction: 'outbound', agent_id: 'lead' });
    expect(url.searchParams.has('before')).toBe(false);
  });

  it('surfaces unavailable archives as errors rather than an empty list', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"Traffic archive is unavailable"}', { status: 503 })));
    await expect(trafficApi.list('task-1', { run: 1 })).rejects.toMatchObject({ status: 503 });
  });

  it('sends native filter expressions in a POST body rather than a logged query string', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const expression = 'http.request && (tcp.port == 443 || ip.addr == 192.0.2.1)';
    await trafficApi.checkFilter('task-1', expression);
    await trafficApi.openNativeQuery('task-1', 2, 'member-1', expression);
    expect(fetch.mock.calls[0][0]).toBe('/api/tasks/task-1/traffic/native/check');
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: 'POST', body: JSON.stringify({ display_filter: expression }) });
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ run: 2, agent_id: 'member-1', display_filter: expression });
  });

  it('pins native pages and frame details to their analysis revision', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const abort = new AbortController();
    await trafficApi.nativePackets('task-1', 'query-1', 'old&revision=other', 0, abort.signal);
    await trafficApi.nativeFrame('task-1', 'query-1', 'revision-1', 'source-a', 7, abort.signal);
    const page = new URL(fetch.mock.calls[0][0], 'http://example.invalid');
    expect(page.searchParams.get('revision')).toBe('old&revision=other');
    expect(page.searchParams.get('offset')).toBe('0');
    expect(fetch.mock.calls[0][1].signal).toBe(abort.signal);
    expect(fetch.mock.calls[1][0]).toBe('/api/tasks/task-1/traffic/native/queries/query-1/sources/source-a/frames/7?revision=revision-1');
  });

  it('shows native syntax errors as invalid input while keeping infrastructure errors distinct', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{"detail":"Filter invalid - Unexpected end of filter expression."}', { status: 422 })).mockResolvedValueOnce(new Response('{"detail":"Native engine unavailable"}', { status: 503 })));
    await expect(trafficApi.checkFilter('task-1', 'http &&')).resolves.toMatchObject({ valid: false, error: 'Filter invalid - Unexpected end of filter expression.' });
    await expect(trafficApi.checkFilter('task-1', 'http')).rejects.toMatchObject({ status: 503 });
  });
});

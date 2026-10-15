import { describe, expect, it, vi } from 'vitest';
import type { TrafficPage, TrafficRecord } from '../api/traffic';
import { appendPackets, nativeTrafficRow, packetHexRows, readPacketUpdates, trafficRow } from './trafficPackets';

const packet = (seq: number) => ({ seq, id: `r-${seq}` }) as TrafficRecord;
const response = (values: Partial<TrafficPage>): TrafficPage => ({ task_id: 'task-1', run: 1, availability: 'recorded', records: [], files: [], has_more: false, snapshot_seq: 0, ...values });

describe('packet display window', () => {
  it('keeps newest packets ordered without duplicate retries or unbounded history', () => {
    const rows = appendPackets([packet(3), packet(8)], [packet(8), packet(12), packet(9)], 3);
    expect(rows.map(row => row.seq)).toEqual([8, 9, 12]);
    expect(appendPackets(rows, [packet(13)], 3).map(row => row.seq)).toEqual([9, 12, 13]);
  });

  it('shows original hex, printable ASCII and byte offsets without decoding payloads', () => {
    const rows = packetHexRows('00014142617f80ff313233343536373841');
    expect(rows).toEqual([
      { offset: 0, bytes: ['00', '01', '41', '42', '61', '7f', '80', 'ff', '31', '32', '33', '34', '35', '36', '37', '38'], ascii: '··ABa···12345678' },
      { offset: 16, bytes: ['41'], ascii: 'A' },
    ]);
    expect(packetHexRows('')).toEqual([]);
    expect(packetHexRows('not-hex')).toEqual([]);
    expect(packetHexRows('001')).toEqual([]);
  });

  it('retries from the original cursor when a later catch-up batch fails', async () => {
    const page = (records: number[], more: boolean, after: number) => response({ records: records.map(packet), has_more: more, next_after: after, snapshot_seq: 10 });
    const read = vi.fn().mockResolvedValueOnce(page([3, 4], true, 4)).mockRejectedValueOnce(new Error('503')).mockResolvedValueOnce(page([3, 4], true, 4)).mockResolvedValueOnce(page([6, 8], false, 10));
    let cursor = 2;
    await expect(readPacketUpdates(read, cursor)).rejects.toThrow('503');
    expect(cursor).toBe(2);
    const retry = await readPacketUpdates(read, cursor);
    cursor = retry.after;
    expect(retry.records.map(record => record.seq)).toEqual([3, 4, 6, 8]);
    expect(cursor).toBe(10);
    expect(read.mock.calls.map(([after]) => after)).toEqual([2, 4, 2, 4]);
  });

  it('starts at the published snapshot and bounds catch-up turns', async () => {
    const initial = await readPacketUpdates(async () => response({ records: [packet(10), packet(8)], snapshot_seq: 12 }));
    expect(initial.records.map(record => record.seq)).toEqual([8, 10]);
    expect(initial.after).toBe(12);
    const read = vi.fn(async (after = 0) => response({ records: [packet(after + 1)], has_more: true, next_after: after + 1, snapshot_seq: 99 }));
    const update = await readPacketUpdates(read, 12);
    expect(read).toHaveBeenCalledTimes(4);
    expect(update.records.map(record => record.seq)).toEqual([13, 14, 15, 16]);
    await expect(readPacketUpdates(async () => response({ has_more: true, next_after: 12 }), 12)).rejects.toThrow('游标无效');
  });

  it('keeps capture source numbers distinct from preview IDs and mixed call records', () => {
    const row = trafficRow({ ...packet(902), timestamp: '2026-10-15T03:00:00Z', packet: { source_id: 'source-a', number: 7, frame_length: 72 }, protocol: 'udp', source_ip: '::1', source_port: 5000, destination_ip: '::1', destination_port: 6000, record_kind: 'packet' } as TrafficRecord);
    expect(row.number).toBe(7);
    expect(row.key).toBe('p:source-a:7');
    expect(row.source).toBe('[::1]:5000');
    const call = trafficRow({ ...packet(903), protocol: 'model', record_kind: 'logical', summary: 'synthetic model', source_ip: '', destination_ip: '' } as TrafficRecord);
    expect(call.number).toBe('R903');
    expect(call.length).toBeNull();
    expect(call.kind).toBe('logical');
    const native = nativeTrafficRow({ source_id: 'source-a', source_number: 7, agent_id: 'agent-1', attribution: 'exact', columns: ['7', '1792033200.123456789', '::1', '::1', 'HTTP', '72', 'GET /example'] });
    expect(native.key).toBe(row.key);
    expect(native.timestamp).toMatch(/\.123Z$/);
    expect(native.protocol).toBe('HTTP');
    expect(nativeTrafficRow({ ...native.native!, columns: [] }).timestamp).toBe('');
  });
});

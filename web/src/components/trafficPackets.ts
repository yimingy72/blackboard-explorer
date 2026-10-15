import type { NativeTrafficPacket, TrafficPage, TrafficRecord } from '../api/traffic';

export const PACKET_WINDOW = 500;

export type TrafficRow = {
  key: string;
  number: string | number;
  timestamp: string;
  agentId: string | null;
  attribution: TrafficRecord['attribution'];
  source: string;
  destination: string;
  protocol: string;
  length: number | null;
  info: string;
  kind: 'packet' | 'http' | 'logical' | 'legacy';
  legacy?: TrafficRecord;
  native?: NativeTrafficPacket;
};

function endpoint(ip: string, port?: number | null) {
  return port == null ? ip || '—' : `${ip.includes(':') ? `[${ip}]` : ip}:${port}`;
}

export function trafficRow(record: TrafficRecord): TrafficRow {
  return {
    key: record.packet ? `p:${record.packet.source_id}:${record.packet.number}` : record.id,
    number: record.packet?.number ?? `R${record.seq}`,
    timestamp: record.timestamp, agentId: record.agent_id ?? null, attribution: record.attribution,
    source: endpoint(record.source_ip, record.source_port), destination: endpoint(record.destination_ip, record.destination_port),
    protocol: (record.application_protocol || record.protocol).toUpperCase(), length: record.packet?.frame_length ?? null,
    info: record.packet?.info || [record.method, record.url, record.summary].filter(Boolean).join(' '),
    kind: record.record_kind ?? 'legacy', legacy: record,
  };
}

export function nativeTrafficRow(record: NativeTrafficPacket): TrafficRow {
  const rawTime = record.columns[1] ?? '';
  const epoch = /^-?\d+(?:\.\d+)?$/.test(rawTime) ? Number(rawTime) : NaN;
  return {
    key: `p:${record.source_id}:${record.source_number}`, number: record.source_number,
    timestamp: Number.isFinite(epoch) && Math.abs(epoch * 1000) <= 8640000000000000 ? new Date(epoch * 1000).toISOString() : '',
    agentId: record.agent_id ?? null, attribution: record.attribution,
    source: record.columns[2] || '—', destination: record.columns[3] || '—', protocol: record.columns[4] || '—',
    length: /^\d+$/.test(record.columns[5] ?? '') ? Number(record.columns[5]) : null,
    info: record.columns[6] || '', kind: 'packet', native: record,
  };
}

export async function readPacketUpdates(read: (after?: number) => Promise<TrafficPage>, after?: number) {
  const initial = after === undefined;
  const records: TrafficRecord[] = [];
  let cursor = after;
  let page: TrafficPage;
  // Return the records and their cursor together; a later failed request must not skip a successful batch.
  for (let batch = 0; batch < 4; batch += 1) {
    page = await read(cursor);
    records.push(...(initial ? [...(page.records ?? [])].reverse() : page.records ?? []));
    if (initial) return { page, records, after: page.snapshot_seq };
    const next = page.next_after ?? page.snapshot_seq;
    if (next < cursor! || (page.has_more && next === cursor)) throw new Error('流量增量游标无效，请刷新重试。');
    cursor = next;
    if (!page.has_more || batch === 3) return { page, records, after: cursor };
  }
  throw new Error('无法读取流量增量。');
}

export function appendPackets(current: TrafficRecord[], incoming: TrafficRecord[], limit = PACKET_WINDOW) {
  const unique = new Map(current.map(record => [record.seq, record]));
  for (const record of incoming) unique.set(record.seq, record);
  return [...unique.values()].sort((a, b) => a.seq - b.seq).slice(-limit);
}

export function packetHexRows(hex: string) {
  if (!/^(?:[a-f\d]{2})*$/i.test(hex)) return [];
  const values = hex.match(/.{2}/g) ?? [];
  const rows: Array<{ offset: number; bytes: string[]; ascii: string }> = [];
  for (let offset = 0; offset < values.length; offset += 16) {
    const bytes = values.slice(offset, offset + 16);
    rows.push({ offset, bytes, ascii: bytes.map(value => {
      const byte = Number.parseInt(value, 16);
      return byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : '·';
    }).join('') });
  }
  return rows;
}

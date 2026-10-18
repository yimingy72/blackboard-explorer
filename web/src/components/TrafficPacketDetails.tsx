import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, ConfigProvider, Select, Splitter, Tag, Tooltip, Tree, Typography } from 'antd';
import { DownOutlined, FilterOutlined, RightOutlined } from '@ant-design/icons';
import type { DataNode } from 'antd/es/tree';
import type { NativeTrafficFrame, TrafficDetail, TrafficPacketNode } from '../api/traffic';
import { packetHexRows } from './trafficPackets';
import styles from './TrafficPacketDetails.module.css';

type Props = {
  detail?: TrafficDetail;
  native?: NativeTrafficFrame;
  pending: boolean;
  error?: string | null;
  width: number;
  onFilter: (expression: string) => void;
};
type FieldNode = DataNode & { field: TrafficPacketNode; children?: FieldNode[] };
const protocolLabels: Record<string, string> = { frame: 'Frame', sll: 'Linux cooked capture v2', eth: 'Ethernet II', ip: 'Internet Protocol Version 4', ipv6: 'Internet Protocol Version 6', tcp: 'Transmission Control Protocol', udp: 'User Datagram Protocol', icmp: 'Internet Control Message Protocol', dns: 'Domain Name System', tls: 'Transport Layer Security', http: 'Hypertext Transfer Protocol', data: 'Data' };
const protocolPriority = ['http', 'http2', 'dns', 'icmp', 'icmpv6', 'arp', 'tls', 'tcp', 'udp'];

export default function TrafficPacketDetails({ detail, native, pending, error, width, onFilter }: Props) {
  const [field, setField] = useState<TrafficPacketNode | null>(null);
  const [fieldKey, setFieldKey] = useState<string>();
  const [sourceId, setSourceId] = useState(0);
  const treeViewport = useRef<HTMLDivElement>(null);
  const primaryLabel = useRef<HTMLSpanElement>(null);
  const nodes = useMemo(() => {
    function convert(items: TrafficPacketNode[], prefix = ''): FieldNode[] {
      const preferred = prefix === '' ? protocolPriority.map(name => items.findIndex(item => item.name === name)).find(index => index >= 0) : undefined;
      return items.map((item, index) => {
        const custom = item.label && item.label !== item.name;
        const label = custom ? item.label : protocolLabels[item.name] || item.name.replace(/^([a-z0-9]+)_\1_/, '$1.');
        const title = !custom && item.value != null ? `${label}: ${item.value}` : label;
        return { key: `${prefix}${index}`, field: item, title: <span ref={index === preferred ? primaryLabel : undefined} className={styles.fieldLabel} data-generated={item.generated} data-severity={item.severity} title={title}>{title}</span>, children: item.children?.length ? convert(item.children, `${prefix}${index}.`) : undefined };
      });
    }
    return convert(native?.layers ?? detail?.packet_detail?.layers ?? []);
  }, [native?.layers, detail?.packet_detail?.layers]);
  const sources = useMemo(() => native?.data_sources ?? [{ id: 0, name: 'Frame · 原始帧', length: detail?.packet?.captured_length ?? 0, raw_hex: detail?.packet_detail?.raw_hex ?? '', truncated: detail?.packet_detail?.raw_truncated ?? false }], [native?.data_sources, detail?.packet, detail?.packet_detail]);
  const primaryLayer = protocolPriority.map(name => nodes.findIndex(node => node.field.name === name)).find(index => index >= 0);
  const nativeFrameKey = native ? `${native.source_id}:${native.source_number}` : '';
  const narrow = width < 680;
  useEffect(() => {
    if (!narrow || !nativeFrameKey) return;
    const frame = requestAnimationFrame(() => {
      const viewport = treeViewport.current, label = primaryLabel.current;
      if (viewport && label) viewport.scrollTop = Math.max(0, viewport.scrollTop + label.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 8);
    });
    return () => cancelAnimationFrame(frame);
  }, [nativeFrameKey, narrow]);
  const source = sources.find(item => item.id === sourceId) ?? sources[0];
  const rows = useMemo(() => packetHexRows(source?.raw_hex ?? ''), [source?.raw_hex]);
  useEffect(() => { setField(null); setFieldKey(undefined); setSourceId(0); }, [native?.revision, nativeFrameKey, detail?.id]);
  const hasRange = field?.offset != null && field.length != null && field.data_source_id === source?.id;
  function highlighted(text: string, offset: number, stride: number) {
    if (!hasRange || !field || field.length === 0) return text;
    const start = Math.max(0, field.offset! - offset), end = Math.min(16, field.offset! + field.length! - offset);
    if (end <= start) return text;
    const from = Math.min(text.length, start * stride), to = Math.min(text.length, end * stride - (stride === 3 ? 1 : 0));
    return <>{text.slice(0, from)}<mark>{text.slice(from, to)}</mark>{text.slice(to)}</>;
  }
  function select(node: TrafficPacketNode, key: string) {
    setField(node); setFieldKey(key);
    if (sources.some(item => item.id === node.data_source_id)) setSourceId(node.data_source_id);
  }
  const treePane = <section className={styles.protocolPane} aria-label="协议层详情">
    <div className={styles.paneHeader}><span>协议详情</span>{pending ? <Tag color="processing">原生解析中</Tag> : native ? <Tag color="blue">Wireshark</Tag> : <Tag>实时预览</Tag>}<Tooltip title={field?.filter_expr || '选择字段后使用其原生过滤表达式'}><Button size="small" type="text" icon={<FilterOutlined aria-hidden="true" />} aria-label="使用选中字段过滤" disabled={!field?.filter_expr} onClick={() => field?.filter_expr && onFilter(field.filter_expr)} /></Tooltip></div>
    <div ref={treeViewport} className={styles.treeViewport}>
      {nodes.length ? <ConfigProvider theme={{ components: { Tree: { indentSize: 16, switcherSize: 24, titleHeight: 28 } } }}><Tree blockNode key={native ? `${native.source_id}:${native.source_number}` : detail?.id} treeData={nodes} selectedKeys={fieldKey ? [fieldKey] : []} defaultExpandedKeys={native && primaryLayer != null ? [String(primaryLayer)] : []} virtual={false} switcherIcon={({ expanded }) => expanded ? <DownOutlined aria-label="收起字段" /> : <RightOutlined aria-label="展开字段" />} onSelect={(keys, info) => { if (keys.length) select((info.node as unknown as FieldNode).field, String(keys[0])); }} /></ConfigProvider> : <div className={styles.placeholder}>{pending ? '正在读取完整协议结构' : '此包没有可用的协议结构'}</div>}
    </div>
    <div className={styles.fieldFooter}>{field ? <><code title={field.name}>{field.name}</code><span>{field.offset != null && field.length != null ? `offset ${field.offset} · ${field.length} B` : field.generated ? '生成字段' : '协议字段'}</span><Typography.Text copyable={{ text: field.value || field.label || field.name, tooltips: ['复制字段', '已复制'] }} /></> : <span>{error || (native?.tree_truncated || detail?.packet_detail?.tree_truncated ? '协议树预览已截断' : '选择字段查看对应字节')}</span>}</div>
  </section>;
  const bytePane = <section className={styles.bytesPane} aria-label="数据包字节详情">
    <div className={styles.paneHeader}><span>字节</span><Select size="small" className={styles.sourceSelect} aria-label="字节数据源" value={source?.id} options={sources.map(item => ({ value: item.id, label: item.name }))} onChange={setSourceId} /><Tooltip title="复制当前数据源的字节，原始与解密字节均为私有原始内容。"><Typography.Text copyable={{ text: source?.raw_hex || '', tooltips: ['复制十六进制', '已复制'] }} /></Tooltip></div>
    <div className={styles.hexViewport} tabIndex={0} aria-label="十六进制与 ASCII">
      <div className={styles.hexHeader}><span>OFFSET</span><span>00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f</span><span>ASCII</span></div>
      {rows.length ? rows.map(row => <div className={styles.hexRow} key={row.offset}><span>{row.offset.toString(16).padStart(4, '0')}</span><code>{highlighted(row.bytes.join(' '), row.offset, 3)}</code><code>{highlighted(row.ascii, row.offset, 1)}</code></div>) : <p className={styles.empty}>没有可用字节</p>}
    </div>
    <div className={styles.byteFooter}><span>{(source?.raw_hex.length ?? 0) / 2} / {source?.length ?? 0} B{source?.truncated ? ' · 预览截断' : ''}</span><span>{source?.id === 0 ? '原始捕获' : '解密 / 重组数据源'}</span>{hasRange && field!.offset! >= (source?.raw_hex.length ?? 0) / 2 && <span>所选字段超出预览范围</span>}</div>
  </section>;
  return <div className={styles.details}>
    <Splitter orientation={width < 680 ? 'vertical' : 'horizontal'}>
      <Splitter.Panel defaultSize="48%" min={width < 680 ? 100 : 180}>{treePane}</Splitter.Panel>
      <Splitter.Panel min={width < 680 ? 100 : 180}>{bytePane}</Splitter.Panel>
    </Splitter>
  </div>;
}

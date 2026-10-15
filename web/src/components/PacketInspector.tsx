import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Collapse, ConfigProvider, Dropdown, Empty, Input, Select, Splitter, Table, Tag, Tooltip } from 'antd';
import type { TableRef } from 'antd/es/table';
import { ApiOutlined, ArrowDownOutlined, CheckCircleFilled, ClockCircleOutlined, CloseCircleFilled, CodeOutlined, DownloadOutlined, FilterOutlined, PauseOutlined, PlayCircleOutlined, QuestionCircleOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons';
import { ApiError } from '../api/client';
import { trafficApi, type TrafficFilters, type TrafficPage, type TrafficRecord } from '../api/traffic';
import { useTrafficNative } from '../api/useTrafficNative';
import { formatDate } from '../pages/format';
import { appendPackets, nativeTrafficRow, PACKET_WINDOW, readPacketUpdates, trafficRow, type TrafficRow } from './trafficPackets';
import LoadingState from './LoadingState';
import TrafficMessages from './TrafficMessages';
import TrafficPacketDetails from './TrafficPacketDetails';
import styles from './PacketInspector.module.css';

type Props = { taskId: string; currentRun: number; runs: number[]; agents: { id: string; name: string }[]; initialAgentId?: string | null };
type Feed = { page?: TrafficPage; rows: TrafficRecord[]; pending: TrafficRecord[]; unseen: number; error: Error | null };
const captureLabels = { collecting: '采集中', complete: '已结束', partial: '采集不完整', error: '采集失败', archived: '已归档' };
const examples = [
  { key: 'http', label: 'HTTP / 解密 HTTP', expression: 'http' },
  { key: 'dns', label: 'DNS 查询与响应', expression: 'dns' },
  { key: 'tls', label: 'TLS 握手', expression: 'tls.handshake' },
  { key: 'port', label: 'TCP 端口', expression: 'tcp.port == 443' },
  { key: 'flags', label: 'TCP 建连', expression: 'tcp.flags.syn == 1' },
  { key: 'errors', label: 'HTTP 错误响应', expression: 'http.response.code >= 400' },
];
const blankFeed = (): Feed => ({ rows: [], pending: [], unseen: 0, error: null });

function usePacketFeed(taskId: string, filters: TrafficFilters) {
  const [feed, setFeed] = useState<Feed>(blankFeed);
  const [paused, setPaused] = useState(false);
  const [revision, setRevision] = useState(0);
  const pausedRef = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let cursor: number | undefined;
    async function poll() {
      try {
        const initial = cursor === undefined;
        const update = await readPacketUpdates(after => trafficApi.list(taskId, { ...filters, limit: 200, after }, controller.signal), cursor);
        if (controller.signal.aborted) return;
        cursor = update.after;
        const { page, records: incoming } = update;
        setFeed(previous => initial ? { ...blankFeed(), page, rows: incoming } : pausedRef.current
          ? { ...previous, page, pending: appendPackets(previous.pending, incoming), unseen: previous.unseen + incoming.length, error: null }
          : { ...previous, page, rows: appendPackets(previous.rows, incoming), error: null });
        if (!page.capture?.finished_at || page.has_more) timer = setTimeout(() => void poll(), page.has_more ? 100 : 1000);
      } catch (error) {
        if (controller.signal.aborted) return;
        setFeed(previous => ({ ...previous, error: error instanceof Error ? error : new Error('流量读取失败。') }));
        timer = setTimeout(() => void poll(), 3000);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [taskId, filters, revision]);
  function pause(value: boolean) {
    pausedRef.current = value; setPaused(value);
    if (!value) setFeed(previous => ({ ...previous, rows: appendPackets(previous.rows, previous.pending), pending: [], unseen: 0 }));
  }
  return { ...feed, paused, pause, refresh: () => { pause(false); setRevision(value => value + 1); } };
}

function useSize() {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 1000, height: 300 });
  useEffect(() => {
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => { const { width, height } = entry.contentRect; setSize(previous => previous.width === width && previous.height === height ? previous : { width, height }); });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return { ref: setElement, ...size };
}
function bytes(value: number) { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1024 / 1024).toFixed(1)} MiB`; }
function packetTime(value: string) { const date = new Date(value); return !value || Number.isNaN(date.getTime()) ? '—' : `${date.toLocaleTimeString('zh-CN', { hour12: false })}.${String(date.getMilliseconds()).padStart(3, '0')}`; }
function packetColor(row: TrafficRow) {
  const protocol = row.protocol.toLowerCase();
  return row.kind === 'logical' ? styles.logicalRow : /http/.test(protocol) ? styles.httpRow : /tls|ssl/.test(protocol) ? styles.encryptedRow : /dns/.test(protocol) ? styles.dnsRow : /icmp|arp/.test(protocol) ? styles.controlRow : /tcp/.test(protocol) ? styles.tcpRow : styles.otherRow;
}

function DisplayFilter({ taskId, value, onApply }: { taskId: string; value: string; onApply: (expression: string) => void }) {
  const [draft, setDraft] = useState(value);
  const [checkExpression, setCheckExpression] = useState(value);
  useEffect(() => { setDraft(value); }, [value]);
  useEffect(() => { const timer = setTimeout(() => setCheckExpression(draft.trim()), 350); return () => clearTimeout(timer); }, [draft]);
  const check = useQuery({ queryKey: ['traffic-filter-check', taskId, checkExpression], queryFn: ({ signal }) => trafficApi.checkFilter(taskId, checkExpression, signal), enabled: Boolean(checkExpression), retry: false, staleTime: 300000, gcTime: 300000, refetchOnWindowFocus: false });
  const current = draft.trim() === checkExpression;
  const invalid = current && check.data?.valid === false;
  const valid = current && check.data?.valid === true;
  const icon = !draft.trim() ? <FilterOutlined aria-hidden="true" /> : invalid ? <CloseCircleFilled aria-hidden="true" /> : valid ? <CheckCircleFilled aria-hidden="true" /> : <ClockCircleOutlined aria-hidden="true" />;
  function apply(expression = draft) { const next = expression.trim(); setDraft(next); onApply(next); }
  return <div className={styles.filterBlock}>
    <div className={styles.expressionBar} data-valid={invalid ? 'invalid' : valid ? 'valid' : 'neutral'}>
      <Input aria-label="Wireshark 显示过滤器" aria-invalid={invalid} aria-describedby={invalid ? 'traffic-filter-error' : undefined} variant="borderless" maxLength={4096} value={draft} placeholder="应用显示过滤器，例如 tcp.port == 443 && ip.addr == 192.0.2.1" prefix={<span className={styles.filterIcon}>{icon}</span>} allowClear onChange={event => { setDraft(event.target.value); if (!event.target.value && value) onApply(''); }} onPressEnter={() => !invalid && apply()} />
      <Dropdown trigger={['click']} menu={{ items: examples.map(({ key, label }) => ({ key, label })), onClick: ({ key }) => { const example = examples.find(item => item.key === key); if (example) apply(example.expression); } }}><Tooltip title="过滤示例"><Button type="text" icon={<QuestionCircleOutlined aria-hidden="true" />} aria-label="过滤示例" /></Tooltip></Dropdown>
      <Button type="primary" icon={<RightOutlined aria-hidden="true" />} disabled={invalid} onClick={() => apply()}>应用</Button>
    </div>
    {invalid && <p className={styles.filterError} id="traffic-filter-error" role="alert">{check.data?.error || '显示过滤表达式无效'}</p>}
    {current && checkExpression && check.error && <p className={styles.filterError} role="alert">无法校验过滤器：{check.error.message}</p>}
  </div>;
}

function PacketSession({ taskId, filters, agents, expression, onFilter }: { taskId: string; filters: TrafficFilters; agents: Props['agents']; expression: string; onFilter: (expression: string) => void }) {
  const feed = usePacketFeed(taskId, filters);
  const [selectedRow, setSelectedRow] = useState<TrafficRow | null>(null);
  const [follow, setFollow] = useState(true);
  const [before, setBefore] = useState<number>();
  const [pages, setPages] = useState<Array<number | undefined>>([]);
  const [nativeOffset, setNativeOffset] = useState<number | null>(null);
  const table = useRef<TableRef>(null), userScrollAt = useRef(0);
  const tableSize = useSize(), lowerSize = useSize();
  const filtered = Boolean(expression);
  const capture = feed.page?.capture;
  const ended = Boolean(capture?.finished_at);
  const owner = (agentId: string | null, attribution: TrafficRow['attribution']) => attribution === 'shared' ? '共享网关' : agentId ? agents.find(agent => agent.id === agentId)?.name ?? agentId : '未归属';
  const nativeQuery = useTrafficNative(taskId, filters.run, filters.agentId || '', expression, filtered, !feed.paused && !ended);
  const nativeState = nativeQuery.data;
  const revision = nativeState?.revision;
  const offset = nativeOffset ?? Math.max(0, (nativeState?.matched_packets ?? 0) - 200);
  const nativeListing = useQuery({ queryKey: ['traffic-native-packets', taskId, nativeState?.id, revision, offset], queryFn: ({ signal }) => trafficApi.nativePackets(taskId, nativeState!.id, revision!, offset, signal), enabled: filtered && Boolean(nativeState?.id && revision), retry: false, gcTime: 60000 });
  const history = useQuery({ queryKey: ['traffic-history', taskId, filters, before], queryFn: ({ signal }) => trafficApi.list(taskId, { ...filters, limit: 200, before }, signal), enabled: !filtered && before !== undefined, retry: false });
  const recordId = selectedRow?.legacy?.id;
  const selected = useQuery({ queryKey: ['traffic-record', taskId, filters.run, recordId], queryFn: ({ signal }) => trafficApi.detail(taskId, filters.run, recordId!, signal), enabled: Boolean(recordId), retry: false, gcTime: 60000 });
  const selectedSource = selectedRow?.native?.source_id ?? selectedRow?.legacy?.packet?.source_id;
  const selectedNumber = selectedRow?.native?.source_number ?? selectedRow?.legacy?.packet?.number;
  const detailQuery = useTrafficNative(taskId, filters.run, selectedRow?.agentId || '', '', !filtered && Boolean(selectedSource), !feed.paused && !ended);
  const activeAnalysis = filtered ? nativeQuery : detailQuery;
  const nativeFrame = useQuery({ queryKey: ['traffic-native-frame', taskId, activeAnalysis.data?.id, activeAnalysis.data?.revision, selectedSource, selectedNumber], queryFn: ({ signal }) => trafficApi.nativeFrame(taskId, activeAnalysis.data!.id, activeAnalysis.data!.revision!, selectedSource!, selectedNumber!, signal), enabled: Boolean(selectedSource && selectedNumber && activeAnalysis.data?.revision), retry: false, gcTime: 60000 });
  const staleRevision = [nativeListing.error, nativeFrame.error].some(error => error instanceof ApiError && error.status === 409);
  const refreshAnalysis = activeAnalysis.refresh;
  useEffect(() => { setNativeOffset(null); }, [expression, revision]);
  useEffect(() => { if (staleRevision) void refreshAnalysis(); }, [staleRevision, refreshAnalysis]);
  const rows = filtered ? (nativeListing.data?.records ?? []).map(nativeTrafficRow) : (before === undefined ? feed.rows : [...(history.data?.records ?? [])].reverse()).map(trafficRow);
  const lastKey = rows.at(-1)?.key;
  useEffect(() => {
    if (!follow || feed.paused || !filtered && before !== undefined || lastKey == null) return;
    const frame = requestAnimationFrame(() => table.current?.scrollTo({ index: rows.length - 1 }));
    return () => cancelAnimationFrame(frame);
  }, [follow, feed.paused, before, lastKey, rows.length, tableSize.height, filtered]);
  function latest() { setBefore(undefined); setPages([]); setNativeOffset(null); feed.pause(false); setFollow(true); }
  function refresh() { latest(); feed.refresh(); if (filtered) void nativeQuery.refresh(); if (selectedSource) void activeAnalysis.refresh(); }
  const error = filtered ? nativeQuery.error ?? (nativeState?.status === 'error' ? new Error(nativeState.error || '原生分析失败') : null) ?? (staleRevision ? null : nativeListing.error) : before === undefined ? feed.error : history.error;
  const pending = filtered ? !revision && !error : !feed.page && !error;
  const rawFiles = feed.page?.files ?? [];
  const firstSeq = before === undefined ? feed.rows[0]?.seq : history.data?.records?.at(-1)?.seq;
  return <div className={styles.session}>
    <div className={styles.toolbar}>
      <div className={styles.liveState} role="status"><i data-live={!ended && !feed.paused} /><strong>{feed.paused ? '已暂停查看' : capture ? captureLabels[capture.status] : '任务流量'}</strong>{capture && <span>{capture.record_count.toLocaleString()} 条 · {bytes(capture.captured_bytes)}</span>}{capture?.preview_dropped ? <Tag color="warning">预览丢弃 {capture.preview_dropped}</Tag> : null}{capture?.dropped_packets ? <Tag color="warning">原始丢包 {capture.dropped_packets}</Tag> : null}</div>
      {feed.unseen > 0 && !filtered && <Button type="link" size="small" onClick={latest}>{feed.unseen.toLocaleString()} 条新流量</Button>}
      <div className={styles.toolActions}><Tooltip title="暂停画面，采集继续"><Button size="small" type="text" icon={feed.paused ? <PlayCircleOutlined aria-hidden="true" /> : <PauseOutlined aria-hidden="true" />} onClick={() => feed.pause(!feed.paused)}>{feed.paused ? '继续' : '暂停'}</Button></Tooltip><Tooltip title="跟随最新流量"><Button size="small" type={follow ? 'primary' : 'text'} ghost={follow} icon={<ArrowDownOutlined aria-hidden="true" />} aria-label="跟随最新流量" aria-pressed={follow} onClick={() => { latest(); setFollow(!follow || before !== undefined); }} /></Tooltip><Tooltip title="刷新流量与解析"><Button size="small" type="text" icon={<ReloadOutlined aria-hidden="true" />} aria-label="刷新流量" onClick={refresh} /></Tooltip></div>
    </div>
    {filtered && <div className={styles.analysisStatus} role="status"><FilterOutlined aria-hidden="true" /><code title={expression}>{expression}</code><span>{nativeState?.status === 'building' ? '分析中' : nativeState?.status === 'partial' ? '部分结果' : nativeState?.status === 'error' ? '分析失败' : revision ? `${nativeState?.matched_packets.toLocaleString()} 个匹配包` : '准备分析'}</span><Tooltip title={(nativeState?.coverage ?? []).map(source => `${owner(source.agent_id ?? null, source.agent_id ? 'exact' : 'shared')}：已分析 ${source.packet_count} 包${source.error ? ` · ${source.error}` : ''}`).join('\n') || '正在读取完整捕获数据'}><span className={styles.engine}>Wireshark {nativeState?.engine_version || ''}</span></Tooltip><Button size="small" type="text" aria-label="清除显示过滤器" onClick={() => onFilter('')}>清除</Button></div>}
    {capture?.error && <Alert className={styles.notice} type="warning" showIcon title={capture.error} />}
    {error && <Alert className={styles.notice} type="error" showIcon title={filtered ? '原生筛选失败' : '流量读取失败'} description={error.message} action={<Button size="small" onClick={refresh}>重试</Button>} />}
    {!filtered && feed.page?.availability === 'not_recorded' ? <Empty className={styles.emptyState} image={Empty.PRESENTED_IMAGE_SIMPLE} description="本轮没有流量存档" /> : !pending && error && !rows.length ? null : <div className={styles.workspace}>
      <Splitter key={lowerSize.width < 680 ? 'narrow' : 'wide'} orientation="vertical">
        <Splitter.Panel defaultSize={lowerSize.width < 680 ? '28%' : '55%'} min={140}>
          <div className={styles.tablePane} ref={tableSize.ref} aria-label="统一流量列表" onWheel={() => { userScrollAt.current = Date.now(); }} onTouchMove={() => { userScrollAt.current = Date.now(); }} onPointerDown={() => { userScrollAt.current = Date.now(); }}>
            {pending ? <LoadingState label="流量列表加载中" lines={6} /> : <ConfigProvider theme={{ components: { Table: { cellPaddingBlockSM: 2, cellPaddingInlineSM: 10, cellFontSizeSM: 12, rowHoverBg: '#eaf0f9' } } }}><Table<TrafficRow> ref={table} size="small" virtual rowKey="key" pagination={false} dataSource={rows} scroll={{ x: 1130, y: Math.max(70, tableSize.height - 31) }} locale={{ emptyText: filtered ? '没有符合显示过滤器的数据包' : history.isError ? '历史流量未能读取' : '尚无流量记录' }} onScroll={event => { const body = event.currentTarget; if (Date.now() - userScrollAt.current < 500 && body.scrollHeight - body.clientHeight - body.scrollTop > 50) setFollow(false); }} rowClassName={row => `${styles.packetRow} ${packetColor(row)} ${row.key === selectedRow?.key ? styles.selectedRow : ''}`} onRow={row => ({ onClick: () => setSelectedRow(row), 'aria-selected': row.key === selectedRow?.key })} columns={[
              { title: 'No.', width: 68, render: (_, row) => <Button type="text" className={styles.sequence} aria-label={`查看流量 ${row.key}`} title={row.kind === 'packet' ? '采集源内的帧编号' : '事务或逻辑调用记录'} onClick={() => setSelectedRow(row)}>{row.number}</Button> },
              { title: '时间', width: 110, render: (_, row) => <time title={row.timestamp ? formatDate(row.timestamp) : ''}>{packetTime(row.timestamp)}</time> },
              { title: 'Agent', width: 120, ellipsis: true, render: (_, row) => <span className={styles.owner} title={`${owner(row.agentId, row.attribution)}${row.agentId ? ` · ${row.agentId}` : ''}`}><i data-exact={row.attribution === 'exact'} />{owner(row.agentId, row.attribution)}</span> },
              { title: 'Source · 来源', width: 166, dataIndex: 'source', ellipsis: true },
              { title: 'Destination · 目标', width: 166, dataIndex: 'destination', ellipsis: true },
              { title: 'Protocol', width: 96, render: (_, row) => <span className={styles.protocol}>{row.kind === 'logical' ? <CodeOutlined aria-label="逻辑调用" /> : row.kind === 'http' ? <ApiOutlined aria-label="HTTP 事务" /> : null}{row.protocol}</span> },
              { title: 'Length', width: 70, align: 'right', render: (_, row) => row.length ?? '—' },
              { title: 'Info', ellipsis: true, width: 334, dataIndex: 'info' },
            ]} /></ConfigProvider>}
          </div>
        </Splitter.Panel>
        <Splitter.Panel min={210}>
          <div ref={lowerSize.ref} className={styles.detailsPane}>
            {!selectedRow ? <div className={styles.noSelection}><span className={styles.emptyIcon}><ApiOutlined aria-hidden="true" /></span><strong>选择一条流量</strong><span>查看协议字段、内容与对应字节</span></div> : <>
              <div className={styles.selectionHeader}><span className={styles.protocolBadge}>{nativeFrame.data?.columns[4] || selectedRow.protocol}</span><span className={styles.selectedEndpoints}>{selectedRow.source}<RightOutlined aria-hidden="true" />{selectedRow.destination}</span><span className={styles.selectedIdentity}>{owner(selectedRow.agentId, selectedRow.attribution)} · #{selectedRow.number}</span></div>
              <div className={styles.selectionBody}>{selected.isError ? <Alert className={styles.notice} type="error" title="流量详情读取失败" description={selected.error.message} /> : selected.isPending && !selectedRow.native ? <LoadingState label="流量详情加载中" lines={4} /> : selected.data && (selected.data.request || selected.data.response || selected.data.observation?.scope === 'logical_call') && !selected.data.packet ? <TrafficMessages detail={selected.data} /> : <TrafficPacketDetails detail={selected.data} native={nativeFrame.data} pending={!nativeFrame.data && Boolean(selectedSource) && !nativeFrame.error && !activeAnalysis.error && activeAnalysis.data?.status !== 'error'} error={nativeFrame.error?.message || activeAnalysis.error?.message || activeAnalysis.data?.error} width={lowerSize.width} onFilter={onFilter} />}</div>
            </>}
          </div>
        </Splitter.Panel>
      </Splitter>
    </div>}
    <div className={styles.footer}><span>{filtered ? `${rows.length} 个包${nativeState?.results_truncated ? ' · 结果已达上限' : ''}` : before !== undefined ? '历史流量' : `${rows.length} 条记录${rows.length === PACKET_WINDOW ? ' · 最近 500 条' : ''}`}</span><span className={styles.footerHint}>{selectedRow?.legacy?.packet?.interface ? `接口 ${selectedRow.legacy.packet.interface}` : filtered ? `${nativeState?.coverage?.length ?? 0} 个采集源 · ${(nativeState?.coverage ?? []).reduce((sum, source) => sum + source.packet_count, 0).toLocaleString()} 包已分析` : capture?.last_packet_at ? `最新包 ${packetTime(capture.last_packet_at)}` : ''}</span><Button size="small" disabled={filtered ? offset + 200 >= (nativeState?.matched_packets ?? 0) : !pages.length} onClick={() => { if (filtered) setNativeOffset(Math.min(Math.max(0, (nativeState?.matched_packets ?? 0) - 200), offset + 200)); else { const previous = [...pages]; setBefore(previous.pop()); setPages(previous); } }}>较新</Button><Button size="small" disabled={filtered ? !offset || nativeListing.isFetching : !firstSeq || history.isFetching || (before !== undefined ? !history.data?.has_more : !feed.page?.has_more && rows.length < 200)} onClick={() => { feed.pause(true); setFollow(false); if (filtered) setNativeOffset(Math.max(0, offset - 200)); else { setPages(current => [...current, before]); setBefore(firstSeq); } }}>更早</Button>{(before !== undefined || nativeOffset !== null) && <Button size="small" type="link" onClick={latest}>最新</Button>}</div>
    {!!rawFiles.length && <Collapse className={styles.rawFiles} size="small" items={[{ key: 'pcap', label: `捕获文件 · ${rawFiles.length}`, children: <Table size="small" rowKey="id" dataSource={rawFiles} scroll={{ x: 460, y: 180 }} pagination={{ pageSize: 5, size: 'small', showSizeChanger: false }} columns={[{ title: '文件', dataIndex: 'name', ellipsis: true }, { title: 'Agent', width: 130, render: (_, file) => owner(file.agent_id ?? null, file.agent_id ? 'exact' : 'shared') }, { title: '大小', width: 100, render: (_, file) => bytes(file.size_bytes) }, { title: '', width: 48, render: (_, file) => <Button type="text" icon={<DownloadOutlined aria-hidden="true" />} aria-label={`下载原始包 ${file.name}`} href={trafficApi.download(taskId, filters.run, file.id)} download target="_blank" rel="noopener noreferrer" /> }]} /> }]} />}
  </div>;
}

export default function PacketInspector({ taskId, currentRun, runs, agents, initialAgentId }: Props) {
  const [run, setRun] = useState(currentRun), [agentId, setAgentId] = useState(initialAgentId || ''), [expression, setExpression] = useState('');
  const filters = useMemo(() => ({ run, agentId }), [run, agentId]);
  const options = [...agents];
  if (agentId && !options.some(agent => agent.id === agentId)) options.push({ id: agentId, name: agentId });
  return <div className={styles.inspector}>
    <div className={styles.scopeBar}><span className={styles.scopeLabel}>捕获范围</span><Select size="small" aria-label="流量轮次" value={run} options={[...new Set([currentRun, ...runs])].sort((a, b) => b - a).map(value => ({ value, label: `第 ${value} 轮` }))} onChange={setRun} /><Select size="small" className={styles.agentSelect} aria-label="流量 Agent" showSearch={{ optionFilterProp: 'label' }} value={agentId} options={[{ value: '', label: '全部 Agent' }, ...options.map(agent => ({ value: agent.id, label: agent.name }))]} onChange={setAgentId} /></div>
    <DisplayFilter taskId={taskId} value={expression} onApply={setExpression} />
    <PacketSession key={JSON.stringify(filters)} taskId={taskId} filters={filters} agents={agents} expression={expression} onFilter={setExpression} />
  </div>;
}

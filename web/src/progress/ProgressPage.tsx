import { Alert, Button, Drawer, Input, Modal, Tag } from 'antd';
import { ApartmentOutlined, ArrowLeftOutlined, FileTextOutlined, MessageOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, type TaskView } from '../api/client';
import { randomUUID } from '../api/uuid';
import type { BoardEvent } from '../board/types';
import InspectorLayout from '../components/InspectorLayout';
import InspectorActions from '../components/InspectorActions';
import LoadingState from '../components/LoadingState';
import ElapsedTime from '../components/ElapsedTime';
import ProgressEvidence from './ProgressEvidence';
import Markdown from '../ctf/Markdown';
import { formatMoney, taskStatusLabel, taskTitle } from '../pages/format';
import { progressApi } from './api';
import type { ProgressRef } from './types';
import styles from './ProgressPage.module.css';
const ProgressCanvas = lazy(() => import('./ProgressCanvas'));
const ProgressDetails = lazy(() => import('./ProgressDetails'));
const ProgressConversation = lazy(() => import('./ProgressConversation'));

export type ProgressSendIntent = { message: { id: string; content: string; node_id: string | null } | null; revision: number; busy: boolean };
export type ProgressUiState = { selected: string | null; panel: 'detail' | 'chat' | null; draft: string; sending: boolean; intent: ProgressSendIntent };
const statuses: Record<string, string> = { idle: '等待新进展', queued: '有新进展待整理', processing: '正在整理', running: '正在整理', budget_exhausted: '整理额度不足以预留下一次调用', unknown_usage: '用量待确认', paused: '整理已暂停', failed: '整理暂时失败', disabled: '未启用' };

export default function ProgressPage({ taskId, task, events, historicalVersion, savedUi, onUiChange }: {
  taskId: string; task: TaskView; events: BoardEvent[]; historicalVersion: number | null;
  savedUi: ProgressUiState; onUiChange: (value: Partial<ProgressUiState>) => void;
}) {
  const navigate = useNavigate(), location = useLocation(), client = useQueryClient();
  const { selected, panel, draft, sending, intent } = savedUi;
  const setSelected = useCallback((value: string | null) => onUiChange({ selected: value }), [onUiChange]);
  const setPanel = useCallback((value: ProgressUiState['panel']) => onUiChange({ panel: value }), [onUiChange]);
  const setDraft = useCallback((value: string) => { intent.revision += 1; onUiChange({ draft: value }); }, [intent, onUiChange]);
  const setSending = useCallback((value: boolean) => onUiChange({ sending: value }), [onUiChange]);
  const [configOpen, setConfigOpen] = useState(false), [cap, setCap] = useState('2'), [busy, setBusy] = useState(false), [error, setError] = useState(String(location.state?.progressSetupError ?? ''));
  const [source, setSource] = useState<ProgressRef | null>(null), [sourceObject, setSourceObject] = useState<Record<string, unknown> | null>(null), [sourceError, setSourceError] = useState('');
  const actionId = useRef<string | null>(null), latestVersion = useRef(events.at(-1)?.version ?? 0);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null), refreshMessages = useRef(false);
  const history = historicalVersion !== null;
  const queryKey = ['progress', taskId, historicalVersion];
  const query = useQuery({ queryKey, queryFn: ({ signal }) => progressApi.get(taskId, signal, historicalVersion ?? undefined), refetchInterval: history ? false : current => current.state.data?.enabled ? 10000 : false });
  const data = query.data, node = data?.nodes.find(item => item.id === selected) ?? null;
  const pendingUsage = Number(data?.budget.unknown ?? 0) > 0;
  const interrupted = data && ['paused', 'failed', 'unknown_usage', 'budget_exhausted'].includes(data.status);
  const diagnostic = data?.diagnostic;
  const outputLimited = diagnostic?.finish_reason === 'length' || diagnostic?.incomplete_reason === 'max_output_tokens';
  const failureTitle = data?.status === 'budget_exhausted' ? statuses.budget_exhausted : outputLimited ? '模型响应达到输出上限，整理已暂停' : pendingUsage ? interrupted ? '整理已暂停，上一笔请求用量待确认' : '有一笔整理用量待确认' : '模型响应未正常完成，整理已暂停';
  const failureDetail = pendingUsage ? `已确认费用 ${formatMoney(data?.budget.spent, 'CNY')}；${formatMoney(data?.budget.unknown, 'CNY')} 为待确认预留，并非确认消费。继续整理时保留该预留并计入额度。` : '已有成果与会话继续保留，求解任务不受影响。';
  useEffect(() => { if (query.error instanceof ApiError && query.error.status === 401) navigate('/login', { replace: true }); }, [query.error, navigate]);
  useEffect(() => {
    const latest = events.at(-1)?.version ?? 0, prior = latestVersion.current; latestVersion.current = latest;
    const fresh = events.filter(event => event.version > prior);
    if (history || latest <= prior || !fresh.some(event => /^(progress\.|fact\.|intent\.|acceptance\.|task\.(resumed|finished|failed|stopped))/.test(event.type))) return;
    refreshMessages.current ||= fresh.some(event => event.type.startsWith('progress.message.') || event.type === 'progress.turn.failed');
    if (refreshTimer.current !== null) return;
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = null;
      void client.invalidateQueries({ queryKey: ['progress', taskId] });
      if (refreshMessages.current) void client.invalidateQueries({ queryKey: ['progress-messages', taskId] });
      refreshMessages.current = false;
    }, 250);
  }, [events, history, client, taskId]);
  useEffect(() => () => { if (refreshTimer.current !== null) clearTimeout(refreshTimer.current); }, []);
  useEffect(() => {
    if (!source || source.kind === 'evidence') return;
    const controller = new AbortController(); setSourceObject(null); setSourceError('');
    const through = historicalVersion ?? source.read_at ?? data?.source_version ?? source.version;
    void progressApi.source(taskId, source.id, through, controller.signal)
      .then(value => { if (!controller.signal.aborted) setSourceObject(value.object); })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setSourceError(cause instanceof Error ? cause.message : '来源读取失败'); });
    return () => controller.abort();
  }, [source, historicalVersion, data?.source_version, taskId]);
  async function configure(enabled: boolean) {
    const value = Number(cap); if (!Number.isFinite(value) || value <= 0) { setError('请填写大于0的独立整理上限。'); return; }
    setBusy(true); setError('');
    try { const saved = await progressApi.configure(taskId, { enabled, max_cost: cap }); client.setQueryData(queryKey, saved); setConfigOpen(false); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '设置失败。'); }
    finally { setBusy(false); }
  }
  async function refresh() {
    if (busy || history) return; setBusy(true); setError('');
    actionId.current ??= randomUUID();
    try { await progressApi.refresh(taskId, actionId.current); actionId.current = null; await query.refetch(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '请求失败，重试沿用原请求。'); }
    finally { setBusy(false); }
  }
  function choose(id: string | null) { setSelected(id); setPanel(id ? 'detail' : null); }
  const chatReference = useCallback((kind: 'fact' | 'intent' | 'progress', id: string, sourceVersion: number) => {
    if (kind === 'progress') { setSelected(id); setPanel('detail'); }
    else setSource({ kind, id, version: sourceVersion || data?.source_version || 0, read_at: sourceVersion || data?.source_version || 0 });
  }, [data?.source_version, setSelected, setPanel]);
  const contents = panel && <aside className={styles.panel} aria-label="成果详情与对话">
    <header className={styles.panelHeader}><span className={styles.panelMark}>{panel === 'chat' ? <MessageOutlined /> : <FileTextOutlined />}</span><div><small>{panel === 'chat' ? '本任务专属会话' : '阶段进展 · AI 整理'}</small><h2>{node?.title ?? '成果整理 Agent'}</h2></div><InspectorActions onClose={() => setPanel(null)} /></header>
    <div className={styles.panelTabs} role="tablist" aria-label="成果面板"><button role="tab" aria-selected={panel === 'detail'} onClick={() => setPanel('detail')}>成果详情</button><button role="tab" aria-selected={panel === 'chat'} disabled={history} onClick={() => setPanel('chat')}>问整理 Agent</button></div>
    <Suspense fallback={<LoadingState label="成果内容" lines={5} />}>{panel === 'chat' ? history ? <p className={styles.chatEmpty}>历史模式只读，返回实时视图后可继续提问。</p> : <ProgressConversation taskId={taskId} node={node} enabled={Boolean(data?.enabled)} draft={draft} onDraftChange={setDraft} onClearNode={() => setSelected(null)} onReference={chatReference} intent={intent} sending={sending} onSendingChange={setSending} /> : node ? <ProgressDetails node={node} onSource={setSource} onAgent={id => navigate(`/tasks/${taskId}?focus=${encodeURIComponent(id)}`)} onAsk={() => { if (!history) setPanel('chat'); }} /> : <div className={styles.chatEmpty}>选择一个成果节点，查看结论、过程与依据。</div>}</Suspense>
  </aside>;
  return <div className={styles.page}>
    <header className={styles.header}><Button type="text" icon={<ArrowLeftOutlined />} aria-label="返回协作黑板" onClick={() => navigate(`/tasks/${taskId}`)} /><h1 title={task.goal}>{taskTitle(task)}</h1><code className={styles.id}>{taskId.slice(0, 8)}</code><Tag color={task.status === 'running' ? 'processing' : 'default'}>{taskStatusLabel(task.status)}</Tag><div className={styles.meta}><span>运行 <ElapsedTime seconds={task.active_seconds} since={task.active_since} /></span><span>求解 {formatMoney(task.usage?.cost, task.cost_currency)} / {formatMoney(task.budget.max_cost, task.cost_currency)}</span>{data?.enabled && <><span>整理 {formatMoney(data.budget.spent, 'CNY')} / {formatMoney(data.budget.max_cost, 'CNY')}</span>{pendingUsage && <span>待确认预留 {formatMoney(data.budget.unknown, 'CNY')}</span>}{task.cost_currency === 'CNY' && <span>已记录合计 {formatMoney(Number(task.usage?.cost ?? 0) + Number(data.budget.spent), 'CNY')}</span>}</>}</div><span className={styles.spacer}/><Button type="text" size="small" icon={<SettingOutlined />} aria-label="成果整理设置" disabled={history} onClick={() => { setCap(data?.budget.max_cost ?? '2'); setConfigOpen(true); }}>设置</Button></header>
    <div className={styles.bar}><nav className={styles.nav} aria-label="任务视图"><Link to={`/tasks/${taskId}`}>协作黑板</Link><Link aria-current="page" to={`/tasks/${taskId}/progress`}><ApartmentOutlined />成果树</Link></nav><span className={styles.watermark}><i />整理至 v{data?.source_version ?? 0}{data && data.desired_source_version > data.source_version ? ' · 有新进展待整理' : ''}</span><span className={styles.spacer}/>{data?.enabled && <Button aria-label="整理最新进展" type="text" size="small" icon={<ReloadOutlined />} disabled={history || busy} loading={busy} onClick={() => void refresh()}><span className={styles.refreshText}>{interrupted ? '继续整理' : '整理最新进展'}</span></Button>}<Button aria-label="问整理 Agent" type="primary" size="small" icon={<MessageOutlined />} disabled={!data?.enabled || history} onClick={() => setPanel('chat')}>问整理 Agent</Button></div>
    {history && <Alert type="info" title={`历史成果 · v${historicalVersion}，只读`} />}
    {(error || query.error) && <Alert type="error" title={error || query.error?.message} action={<Button onClick={() => void query.refetch()}>重试读取</Button>} />}
    {(data?.error || interrupted || pendingUsage) && <Alert type="warning" showIcon title={failureTitle} description={<>{failureDetail}{data?.error && data.error !== 'ModelStreamError' && <p>{data.error}</p>}{diagnostic && <p>{[diagnostic.event_type, diagnostic.incomplete_reason ?? diagnostic.finish_reason, diagnostic.provider_code, diagnostic.request_id ?? diagnostic.provider_response_id].filter(Boolean).join(' · ')}</p>}</>} />}
    <div className={styles.stage}>
      {query.isLoading ? <LoadingState page label="成果树" lines={6} /> : data && !data.enabled && data.revision === 0 ? <div className={styles.empty}><section className={styles.setup}><h2>把探索记录整理成可读成果</h2><p>根据整体目标归并方向，说明关键发现、排除结果和未解决问题。独立整理 Agent 只读黑板，不改变原任务预算与执行。</p><label>额外整理上限（人民币）<Input type="number" min="0.01" step="any" prefix="¥" aria-label="独立整理费用上限" value={cap} onChange={event => setCap(event.target.value)} style={{ marginBlock: 10 }} /></label><Button type="primary" disabled={busy || history} loading={busy} onClick={() => void configure(true)}>启用成果整理</Button></section></div> : data ? <>
        {data.overview && <div className={styles.summary}><strong>当前认识</strong><p>{data.overview}</p></div>}
        {!data.enabled && <Alert type="info" title="自动整理已关闭，保留现有成果" />}
        <InspectorLayout inspector={contents} label="成果详情与对话" onClose={() => setPanel(null)}><Suspense fallback={<LoadingState page label="成果画布" lines={5} />}><ProgressCanvas taskId={taskId} nodes={data.nodes} selectedId={selected} onSelect={choose} historical={history} /></Suspense></InspectorLayout>
      </> : null}
    </div>
    <Modal title="成果整理设置" open={configOpen} onCancel={() => { if (!busy) setConfigOpen(false); }} footer={<><Button disabled={busy} onClick={() => void configure(false)}>关闭自动整理</Button><Button type="primary" loading={busy} onClick={() => void configure(true)}>保存并启用</Button></>}><p className={styles.modelInfo}>上限独立于求解预算，包含自动整理和提问；降低上限不会删除已发生费用。</p><label>独立金额上限<Input type="number" aria-label="修改独立整理上限" prefix="¥" value={cap} onChange={event => setCap(event.target.value)} /></label><dl className={styles.modelSettings}><div><dt>模型</dt><dd>{data?.model?.model ?? '沿用任务模型'}</dd></div><div><dt>单次最大输出</dt><dd>{data?.model_limits ? `${data.model_limits.max_output_tokens.toLocaleString()} token` : '沿用模型配置'}</dd></div><div><dt>推理强度</dt><dd>{data?.model_limits?.reasoning_effort ?? data?.model?.reasoning_effort ?? '沿用模型配置'}</dd></div></dl><Link to="/profiles?section=models">管理模型配置</Link>{error && <Alert type="error" title={error} />}</Modal>
    <Drawer title={source ? `${source.id} · 原始依据` : '原始依据'} open={Boolean(source)} onClose={() => setSource(null)} size={480}>
      {source?.kind === 'evidence' ? <ProgressEvidence taskId={taskId} source={source} /> : sourceError ? <Alert type="error" title={sourceError} /> : sourceObject ? <div className={styles.source}><Markdown text={String(sourceObject.statement ?? sourceObject.goal ?? sourceObject.desc ?? '原始记录')} /><p className={styles.muted}>作者：{String(sourceObject.author ?? '系统')} · 来源版本 v{String(sourceObject.version ?? source?.version)}</p>{typeof sourceObject.expected === 'string' && <p>预期：{sourceObject.expected}</p>}{typeof sourceObject.method === 'string' && <p>方法：{sourceObject.method}</p>}{typeof sourceObject.status === 'string' && <p>状态：{sourceObject.status}{sourceObject.result ? ` · ${String(sourceObject.result)}` : ''}</p>}{Array.isArray(sourceObject.evidence) && sourceObject.evidence.map((raw: Record<string, unknown>, index) => <ProgressEvidence key={index} taskId={taskId} kind={String(raw.type ?? 'text')} source={{ kind: 'evidence', id: String(raw.summary ?? `证据 ${index + 1}`), evidence_uri: String(raw.registered_uri ?? ''), sha256: typeof raw.sha256 === 'string' ? raw.sha256 : null, version: Number(raw.registration_version ?? sourceObject.version ?? 0) }} />)}<Button onClick={() => navigate(`/tasks/${taskId}?focus=${encodeURIComponent(source?.id ?? '')}`)}>在协作黑板定位</Button></div> : <LoadingState label="来源记录" lines={5} />}
    </Drawer>
  </div>;
}

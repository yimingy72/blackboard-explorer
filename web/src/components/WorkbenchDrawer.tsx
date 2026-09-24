import { useId, useRef, useState, type KeyboardEvent } from 'react';
import type { BoardEvent, BoardState } from '../board/types';
import { eventLabels } from '../board/view';
import { formatCost } from '../pages/format';
import WorkspaceExplorer from './WorkspaceExplorer';
import styles from './WorkbenchDrawer.module.css';

type Tab = 'events' | 'agents' | 'judgments' | 'timeline' | 'workspace';
type Props = {
  taskId: string;
  state: BoardState;
  events: BoardEvent[];
  allEvents: BoardEvent[];
  version: number | null;
  onVersion: (value: number | null) => void;
  onSelect: (id: string | null) => void;
};
const tabs: Array<[Tab, string]> = [
  ['agents', 'Agent 记录'], ['events', '事件流'], ['judgments', '裁定历史'],
  ['timeline', '时间轴'], ['workspace', '工作区'],
];
const statusLabels: Record<string, string> = { running: '运行中', concluding: '交接中', finished: '已结束', failed: '失败' };
const reasonLabels: Record<string, string> = { normal: '正常结束', refused: '拒绝开始', limit: '达到上限', grace_timeout: '交接超时', heartbeat: '心跳超时', runtime_error: '运行错误', runtime_restart: '运行器重启' };
const previewLimit = 200 * 1024;

function payloadPreview(payload: BoardEvent['payload']): { text: string; truncated: boolean; size: number } {
  const raw = JSON.stringify(payload ?? {}, null, 2);
  const bytes = new TextEncoder().encode(raw);
  if (bytes.length <= previewLimit) return { text: raw, truncated: false, size: bytes.length };
  const half = previewLimit / 2;
  const decoder = new TextDecoder();
  return {
    text: `${decoder.decode(bytes.subarray(0, half))}\n\n… 已省略中间内容 …\n\n${decoder.decode(bytes.subarray(bytes.length - half))}`,
    truncated: true,
    size: bytes.length,
  };
}

function EventRow({ event, state, onVersion, onSelect }: {
  event: BoardEvent; state: BoardState; onVersion: Props['onVersion']; onSelect: Props['onSelect'];
}) {
  const [expanded, setExpanded] = useState(false);
  const preview = expanded ? payloadPreview(event.payload) : null;
  const objectId = event.object_id;
  const selectable = objectId && (state.facts[objectId] || state.intents[objectId] || state.agents[objectId]);
  return <li><details onToggle={(value) => setExpanded(value.currentTarget.open)}>
    <summary><span className={styles.version}>v{event.version}</span><strong>{eventLabels[event.type] ?? event.type}</strong><span>{event.actor}</span><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time></summary>
    <div className={styles.eventActions}><button type="button" onClick={() => onVersion(event.version)}>回放到此版本</button>{selectable && <button type="button" onClick={() => onSelect(objectId)}>查看 {objectId}</button>}</div>
    {preview && <>{preview.truncated && <p className={styles.previewNotice}>事件内容共 {preview.size.toLocaleString()} 字节；仅显示开头与结尾各 100 KiB。</p>}<pre tabIndex={0}>{preview.text}</pre></>}
  </details></li>;
}

function AgentRecords({ state, onSelect }: { state: BoardState; onSelect: Props['onSelect'] }) {
  const agents = Object.values(state.agents);
  if (!agents.length) return <p className={styles.empty}>当前版本还没有 Agent 运行记录。</p>;
  return <div className={styles.tableScroll}><table><thead><tr><th>Agent</th><th>任务</th><th>状态 / 结束原因</th><th>模型调用</th><th>上下文</th><th>输出 / 推理 token</th><th>花费</th><th>当前意图</th></tr></thead><tbody>
    {agents.map((agent) => <tr key={agent.id}><td><button type="button" onClick={() => onSelect(agent.id)}>{agent.id}</button></td><td>{agent.isSeed ? '种子探索' : agent.taskType === 'explore' ? '探索' : agent.taskType === 'derive' ? '推导' : agent.closeMode === 'final' ? '终结' : '裁定'}</td><td>{statusLabels[agent.status]}{agent.endReason && <small>{reasonLabels[agent.endReason] ?? agent.endReason}</small>}</td><td>{agent.steps}</td><td>{agent.contextTokens.toLocaleString()}</td><td>{agent.usage.output_tokens ?? 0} / {agent.usage.reasoning_tokens ?? 0}</td><td>{formatCost(agent.usage.cost ?? 0)}</td><td>{agent.intentId ? <button type="button" onClick={() => onSelect(agent.intentId)}>{agent.intentId}</button> : '—'}</td></tr>)}
  </tbody></table></div>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function Judgments({ events, onVersion, onSelect }: {
  events: BoardEvent[]; onVersion: Props['onVersion']; onSelect: Props['onSelect'];
}) {
  const judgments = events.filter((event) => event.type === 'acceptance.judged' || event.type === 'acceptance.reverted').reverse();
  if (!judgments.length) return <p className={styles.empty}>当前版本尚无裁定。</p>;
  return <div className={styles.judgments}>{judgments.map((event) => {
    const payload = event.payload ?? {};
    const verdicts = Array.isArray(payload.verdicts) ? payload.verdicts.filter(isRecord) : [];
    return <article key={event.version}><header><strong>{event.type === 'acceptance.reverted' ? '验收回退' : payload.mode === 'final' ? '终结裁定' : '探索裁定'}</strong><button type="button" onClick={() => onVersion(event.version)}>v{event.version}</button><span>{event.actor}</span></header>
      {event.type === 'acceptance.reverted' ? <p>{String(payload.id ?? '未知验收项')} 的支撑事实 {String(payload.fact_id ?? '未知事实')} 被争议，验收回到未满足。</p> : verdicts.length ? verdicts.map((item, index) => {
        const evidenceFacts = Array.isArray(item.evidence_facts) ? item.evidence_facts.filter((id): id is string => typeof id === 'string') : [];
        return <div key={`${String(item.id ?? '')}-${index}`}><h3>{String(item.id ?? '未知验收项')} · {item.verdict === 'met' ? '已满足' : '未满足'}</h3><p>{String(item.reason ?? '')}</p>{typeof item.missing === 'string' && item.missing && <p>缺口：{item.missing}</p>}<div className={styles.factLinks}>{evidenceFacts.map((id) => <button key={id} type="button" onClick={() => onSelect(id)}>{id}</button>)}</div></div>;
      }) : <p className={styles.empty}>这次裁定没有逐项结论。</p>}
    </article>;
  })}</div>;
}

export default function WorkbenchDrawer({ taskId, state, events, allEvents, version, onVersion, onSelect }: Props) {
  const [tab, setTab] = useState<Tab>('agents');
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(100);
  const [eventFilter, setEventFilter] = useState('');
  const tabRefs = useRef<Partial<Record<Tab, HTMLButtonElement | null>>>({});
  const id = useId();
  const filtered = events.filter((event) => !eventFilter || event.type.startsWith(eventFilter));
  const last = allEvents.at(-1)?.version ?? 0;
  const current = version ?? last;
  const panelId = `${id}-panel`;
  function keySwitch(event: KeyboardEvent<HTMLDivElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = tabs.findIndex(([value]) => value === tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    const nextTab = tabs[next][0];
    setTab(nextTab);
    setOpen(true);
    tabRefs.current[nextTab]?.focus();
  }
  return <section className={styles.drawer} aria-label="任务运行记录">
    <div className={styles.tabs}>
      <div className={styles.tabList} role="tablist" aria-label="记录分类" onKeyDown={keySwitch}>
        {tabs.map(([name, label]) => <button key={name} ref={(node) => { tabRefs.current[name] = node; }} type="button" role="tab" id={`${id}-tab-${name}`} aria-selected={tab === name} aria-controls={panelId} tabIndex={tab === name ? 0 : -1} onClick={() => { setTab(name); setOpen(true); }}>{label}{name === 'agents' ? ` · ${Object.keys(state.agents).length}` : ''}</button>)}
      </div>
      <button type="button" className={styles.toggle} aria-controls={panelId} aria-expanded={open} onClick={() => setOpen(!open)} aria-label={open ? '收起运行记录' : '展开运行记录'}>{open ? '收起 ↓' : '展开 ↑'}</button>
    </div>
    <div className={styles.content} id={panelId} role="tabpanel" aria-labelledby={`${id}-tab-${tab}`} tabIndex={0} hidden={!open}>
      {open && tab === 'agents' && <AgentRecords state={state} onSelect={onSelect} />}
      {open && tab === 'events' && <>
        <label className={styles.inlineField}>事件类型<select value={eventFilter} onChange={(event) => setEventFilter(event.target.value)}><option value="">全部</option>{['task.', 'agent.', 'fact.', 'intent.', 'acceptance.', 'tool_call.', 'budget.', 'derive.'].map((type) => <option key={type} value={type}>{type}</option>)}</select><span>{filtered.length} 条</span></label>
        {filtered.length ? <ol className={styles.events}>{filtered.slice(-shown).reverse().map((event) => <EventRow key={event.version} event={event} state={state} onVersion={onVersion} onSelect={onSelect} />)}</ol> : <p className={styles.empty}>当前筛选没有事件。</p>}
        {filtered.length > shown && <button type="button" onClick={() => setShown((count) => count + 100)}>再显示 100 条</button>}
      </>}
      {open && tab === 'judgments' && <Judgments events={events} onVersion={onVersion} onSelect={onSelect} />}
      {open && tab === 'timeline' && <div className={styles.timeline}><label htmlFor={`${id}-replay-version`}>{version === null ? '实时版本' : '历史回放'} · v{current}</label><input id={`${id}-replay-version`} type="range" min={allEvents[0]?.version ?? 0} max={last} step="1" value={current} onChange={(event) => onVersion(Number(event.target.value))} aria-label="回放版本" disabled={!allEvents.length} /><p>选择版本后，图谱、详情和运行记录都会还原到当时。新事件继续接收。</p><button type="button" onClick={() => onVersion(null)} disabled={version === null}>返回实时</button><span>{allEvents.length} 个事件 · 当前显示 {events.length} 个</span></div>}
      {open && tab === 'workspace' && <WorkspaceExplorer taskId={taskId} available={Boolean(state.task?.workspace_uri)} />}
    </div>
  </section>;
}

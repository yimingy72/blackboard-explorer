import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import type { BoardEvent, BoardState } from '../board/types';
import { eventLabels } from '../board/view';
import { agentLabel } from '../board/agents';
import WorkspaceExplorer from './WorkspaceExplorer';
import styles from './WorkbenchDrawer.module.css';

type Tab = 'events' | 'judgments' | 'timeline' | 'workspace';
type Props = {
  open: boolean;
  onClose: () => void;
  taskId: string;
  state: BoardState;
  events: BoardEvent[];
  allEvents: BoardEvent[];
  version: number | null;
  onVersion: (value: number | null) => void;
  onSelect: (id: string | null) => void;
  agentNumbers: Record<string, number>;
};
const tabs: Array<[Tab, string]> = [
  ['events', '事件流'], ['judgments', '裁定历史'],
  ['timeline', '时间轴'], ['workspace', '工作区'],
];
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

function EventRow({ event, state, onVersion, onSelect, agentNumbers }: {
  event: BoardEvent; state: BoardState; onVersion: Props['onVersion']; onSelect: Props['onSelect']; agentNumbers: Props['agentNumbers'];
}) {
  const [expanded, setExpanded] = useState(false);
  const preview = expanded ? payloadPreview(event.payload) : null;
  const objectId = event.object_id;
  const selectable = objectId && (state.facts[objectId] || state.intents[objectId] || state.agents[objectId]);
  return <li><details onToggle={(value) => setExpanded(value.currentTarget.open)}>
    <summary><span className={styles.version}>v{event.version}</span><strong>{eventLabels[event.type] ?? event.type}</strong><span>{state.agents[event.actor] ? agentLabel(event.actor, agentNumbers) : event.actor}</span><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time></summary>
    <div className={styles.eventActions}><button type="button" onClick={() => onVersion(event.version)}>回放到此版本</button>{selectable && <button type="button" onClick={() => onSelect(objectId)}>查看 {state.agents[objectId] ? agentLabel(objectId, agentNumbers) : objectId}</button>}</div>
    {preview && <>{preview.truncated && <p className={styles.previewNotice}>事件内容共 {preview.size.toLocaleString()} 字节；仅显示开头与结尾各 100 KiB。</p>}<pre tabIndex={0}>{preview.text}</pre></>}
  </details></li>;
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

export default function WorkbenchDrawer({ open, onClose, taskId, state, events, allEvents, version, onVersion, onSelect: selectObject, agentNumbers }: Props) {
  const [tab, setTab] = useState<Tab>('events');
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open) dialog.current?.close();
  }, [open]);
  const onSelect = (id: string | null) => { selectObject(id); onClose(); };
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
    tabRefs.current[nextTab]?.focus();
  }
  return <dialog ref={dialog} className={styles.drawer} aria-label="复盘记录" onCancel={onClose} onClose={onClose}>
    <header className={styles.dialogHeader}><h2>复盘记录</h2><button type="button" onClick={onClose} aria-label="关闭复盘记录">×</button></header>
    <div className={styles.tabs}>
      <div className={styles.tabList} role="tablist" aria-label="记录分类" onKeyDown={keySwitch}>
        {tabs.map(([name, label]) => <button key={name} ref={(node) => { tabRefs.current[name] = node; }} type="button" role="tab" id={`${id}-tab-${name}`} aria-selected={tab === name} aria-controls={panelId} tabIndex={tab === name ? 0 : -1} onClick={() => { setTab(name); }}>{label}</button>)}
      </div>
    </div>
    <div className={styles.content} id={panelId} role="tabpanel" aria-labelledby={`${id}-tab-${tab}`} tabIndex={0} hidden={!open}>
      {open && tab === 'events' && <>
        <label className={styles.inlineField}>事件类型<select value={eventFilter} onChange={(event) => setEventFilter(event.target.value)}><option value="">全部</option>{['task.', 'agent.', 'fact.', 'intent.', 'acceptance.', 'tool_call.', 'budget.', 'derive.'].map((type) => <option key={type} value={type}>{type}</option>)}</select><span>{filtered.length} 条</span></label>
        {filtered.length ? <ol className={styles.events}>{filtered.slice(-shown).reverse().map((event) => <EventRow key={event.version} event={event} state={state} onVersion={onVersion} onSelect={onSelect} agentNumbers={agentNumbers} />)}</ol> : <p className={styles.empty}>当前筛选没有事件。</p>}
        {filtered.length > shown && <button type="button" onClick={() => setShown((count) => count + 100)}>再显示 100 条</button>}
      </>}
      {open && tab === 'judgments' && <Judgments events={events} onVersion={onVersion} onSelect={onSelect} />}
      {open && tab === 'timeline' && <div className={styles.timeline}><label htmlFor={`${id}-replay-version`}>{version === null ? '实时版本' : '历史回放'} · v{current}</label><input id={`${id}-replay-version`} type="range" min={allEvents[0]?.version ?? 0} max={last} step="1" value={current} onChange={(event) => onVersion(Number(event.target.value))} aria-label="回放版本" disabled={!allEvents.length} /><p>选择版本后，图谱、详情和运行记录都会还原到当时。新事件继续接收。</p><button type="button" onClick={() => onVersion(null)} disabled={version === null}>返回实时</button><span>{allEvents.length} 个事件 · 当前显示 {events.length} 个</span></div>}
      {open && tab === 'workspace' && <WorkspaceExplorer taskId={taskId} available={Boolean(state.task?.workspace_uri)} />}
    </div>
  </dialog>;
}

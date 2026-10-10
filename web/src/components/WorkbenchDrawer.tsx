import { Button, Collapse, Modal, Select, Slider, Tabs } from 'antd';
import { useRef, useState } from 'react';
import type { BoardEvent, BoardState } from '../board/types';
import { eventLabels } from '../board/view';
import { agentLabel } from '../board/agents';
import WorkspaceExplorer from './WorkspaceExplorer';
import styles from './WorkbenchDrawer.module.css';
type Tab = 'events' | 'judgments' | 'timeline' | 'workspace';
type Props = {
    open: boolean;
    onClose: () => void;
    onRestoreFocus: () => void;
    taskId: string;
    state: BoardState;
    events: BoardEvent[];
    allEvents: BoardEvent[];
    version: number | null;
    onVersion: (value: number | null) => void;
    onSelect: (id: string | null) => void;
    agentNumbers: Record<string, number>;
};
const tabs: Array<[
    Tab,
    string
]> = [
    ['events', '事件流'], ['judgments', '裁定历史'],
    ['timeline', '时间轴'], ['workspace', '工作区'],
];
const previewLimit = 200 * 1024;
function payloadPreview(payload: BoardEvent['payload']): {
    text: string;
    truncated: boolean;
    size: number;
} {
    const raw = JSON.stringify(payload ?? {}, null, 2);
    const bytes = new TextEncoder().encode(raw);
    if (bytes.length <= previewLimit)
        return { text: raw, truncated: false, size: bytes.length };
    const half = previewLimit / 2;
    const decoder = new TextDecoder();
    return {
        text: `${decoder.decode(bytes.subarray(0, half))}\n\n… 已省略中间内容 …\n\n${decoder.decode(bytes.subarray(bytes.length - half))}`,
        truncated: true,
        size: bytes.length,
    };
}
function EventRow({ event, state, onVersion, onSelect, agentNumbers }: {
    event: BoardEvent;
    state: BoardState;
    onVersion: Props['onVersion'];
    onSelect: Props['onSelect'];
    agentNumbers: Props['agentNumbers'];
}) {
    const [expanded, setExpanded] = useState(false);
    const preview = expanded ? payloadPreview(event.payload) : null;
    const objectId = event.object_id;
    const selectable = objectId && (state.facts[objectId] || state.intents[objectId] || state.agents[objectId]);
    return <li><Collapse ghost size="small" activeKey={expanded?['event']:[]} onChange={keys=>setExpanded(keys.length>0)} items={[{key:'event',label:<span className={styles.eventLabel}><span className={styles.version}>v{event.version}</span><strong>{eventLabels[event.type]??event.type}</strong><span>{state.agents[event.actor]?agentLabel(event.actor,agentNumbers):event.actor}</span><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time></span>,children:<><div className={styles.eventActions}><Button size="small" onClick={()=>onVersion(event.version)}>回放到此版本</Button>{selectable&&<Button size="small" onClick={()=>onSelect(objectId)}>查看 {state.agents[objectId]?agentLabel(objectId,agentNumbers):objectId}</Button>}</div>{preview&&<>{preview.truncated&&<p className={styles.previewNotice}>事件内容共 {preview.size.toLocaleString()} 字节；仅显示开头与结尾各 100 KiB。</p>}<pre tabIndex={0}>{preview.text}</pre></>}</>}]} /></li>;
}
function isRecord(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function Judgments({ events, onVersion, onSelect }: {
    events: BoardEvent[];
    onVersion: Props['onVersion'];
    onSelect: Props['onSelect'];
}) {
    const judgments = events.filter((event) => event.type === 'acceptance.judged' || event.type === 'acceptance.reverted').reverse();
    if (!judgments.length)
        return <p className={styles.empty}>当前版本尚无裁定。</p>;
    return <div className={styles.judgments}>{judgments.map((event) => {
            const payload = event.payload ?? {};
            const verdicts = Array.isArray(payload.verdicts) ? payload.verdicts.filter(isRecord) : [];
            return <article key={event.version}><header><strong>{event.type === 'acceptance.reverted' ? '验收回退' : payload.mode === 'final' ? '终结裁定' : '探索裁定'}</strong><Button onClick={() => onVersion(event.version)} htmlType={"button"}>v{event.version}</Button><span>{event.actor}</span></header>
      {event.type === 'acceptance.reverted' ? <p>{String(payload.id ?? '未知验收项')} 的支撑事实 {String(payload.fact_id ?? '未知事实')} 被争议，验收回到未满足。</p> : verdicts.length ? verdicts.map((item, index) => {
                    const evidenceFacts = Array.isArray(item.evidence_facts) ? item.evidence_facts.filter((id): id is string => typeof id === 'string') : [];
                    return <div key={`${String(item.id ?? '')}-${index}`}><h3>{String(item.id ?? '未知验收项')} · {item.verdict === 'met' ? '已满足' : '未满足'}</h3><p>{String(item.reason ?? '')}</p>{typeof item.missing === 'string' && item.missing && <p>缺口：{item.missing}</p>}<div className={styles.factLinks}>{evidenceFacts.map((id) => <Button key={id} onClick={() => onSelect(id)} htmlType={"button"}>{id}</Button>)}</div></div>;
                }) : <p className={styles.empty}>这次裁定没有逐项结论。</p>}
    </article>;
        })}</div>;
}
export default function WorkbenchDrawer({open,onClose,onRestoreFocus,taskId,state,events,allEvents,version,onVersion,onSelect:selectObject,agentNumbers}:Props) {
  const [tab,setTab]=useState<Tab>('events');
  const [shown,setShown]=useState(100);
  const [eventFilter,setEventFilter]=useState('');
  const restoreFocus=useRef(true);
  const select=(id:string|null)=>{restoreFocus.current=false;selectObject(id);onClose();};
  const filtered=events.filter(event=>!eventFilter||event.type.startsWith(eventFilter));
  const last=allEvents.at(-1)?.version??0;const current=version??last;
  const content=<div className={styles.content}>
    {open&&tab==='events'&&<><div className={styles.inlineField}><span>事件类型</span><Select aria-label="事件类型" value={eventFilter} onChange={setEventFilter} options={[{value:'',label:'全部'},...['task.','agent.','fact.','intent.','acceptance.','tool_call.','budget.','derive.'].map(value=>({value,label:value}))]} style={{width:180}} /><span>{filtered.length} 条</span></div>{filtered.length?<ol className={styles.events}>{filtered.slice(-shown).reverse().map(event=><EventRow key={event.version} event={event} state={state} onVersion={onVersion} onSelect={select} agentNumbers={agentNumbers} />)}</ol>:<p className={styles.empty}>当前筛选没有事件。</p>}{filtered.length>shown&&<Button onClick={()=>setShown(count=>count+100)}>再显示 100 条</Button>}</>}
    {open&&tab==='judgments'&&<Judgments events={events} onVersion={onVersion} onSelect={select} />}
    {open&&tab==='timeline'&&<div className={styles.timeline}><span>{version===null?'实时版本':'历史回放'} · v{current}</span><Slider tooltip={{formatter:null}} ariaLabelForHandle="回放版本" min={allEvents[0]?.version??0} max={last} step={1} value={current} onChange={value=>onVersion(value)} disabled={!allEvents.length} keyboard /><p>当前视图只展示所选版本，新事件继续接收。</p><Button onClick={()=>onVersion(null)} disabled={version===null}>返回实时</Button><span>{allEvents.length} 个事件 · 当前显示 {events.length} 个</span></div>}
    {open&&tab==='workspace'&&<WorkspaceExplorer taskId={taskId} available={Boolean(state.task?.workspace_uri)} />}
  </div>;
  return <Modal title="复盘记录" open={open} onCancel={()=>{restoreFocus.current=true;onClose();}} afterClose={()=>{if(restoreFocus.current)onRestoreFocus();restoreFocus.current=true;}} footer={null} width={1120} focusable={{focusTriggerAfterClose:false}} styles={{body:{maxHeight:'75dvh',overflow:'auto'}}}><Tabs activeKey={tab} onChange={value=>setTab(value as Tab)} items={tabs.map(([key,label])=>({key,label,children:key===tab?content:null}))} /></Modal>;
}

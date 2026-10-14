import { Button, Collapse, Tabs } from 'antd';
import BlackboardState from './BlackboardState';
import { api } from '../api/client';
import InspectorActions from './InspectorActions';
import { formatMoney } from '../pages/format';
import { TaskCurrency } from '../pages/currency';
import { createContext, useContext, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import type { BoardAgent, BoardFact, BoardIntent, BoardState } from '../board/types';
import { agentLabel } from '../board/agents';
import controls from '../styles/controls.module.css';
import { disputeChain, retryChain } from './detailRelations';
import EvidenceViewer from './EvidenceViewer';
import styles from './DetailPanel.module.css';
import { readableProse } from './readable';
type Props = {
    taskId: string;
    state: BoardState;
    selectedId: string | null;
    onSelect: (id: string | null) => void;
    historical?: boolean;
    agentNumbers?: Record<string, number>;
};
const AgentNumbers = createContext<Record<string, number>>({});
const factKinds: Record<string, string> = { observation: '观察事实', inference: '推断事实', structure: '结构事实' };
const agentStatus: Record<string, string> = { running: '运行中', concluding: '收尾中', finished: '已结束', failed: '失败' };
const resultLabels: Record<string, string> = { confirmed: '确认', rejected: '否定', inconclusive: '未定' };
function MarkdownBody({ children, sourceToggle = false }: {
    children: string;
    sourceToggle?: boolean;
}) {
    const [source, setSource] = useState(false);
    const [copyStatus, setCopyStatus] = useState('');
    async function copy() {
        try {
            await navigator.clipboard.writeText(children);
            setCopyStatus('已复制');
        }
        catch {
            setCopyStatus('请选中原文复制');
        }
    }
    return <div className={styles.markdown}>
    {sourceToggle && <div className={styles.readingTools}><Button aria-pressed={!source} onClick={() => setSource(false)} htmlType={"button"}>阅读排版</Button><Button aria-pressed={source} onClick={() => setSource(true)} htmlType={"button"}>原文</Button><Button onClick={() => void copy()} htmlType={"button"}>复制原文</Button><span role="status">{copyStatus}</span></div>}
    {source ? <pre className={styles.rawText}>{children}</pre> : <ReactMarkdown remarkPlugins={[readableProse]} components={{
                h1: ({ children }) => <h3>{children}</h3>,
                h2: ({ children }) => <h3>{children}</h3>,
                h3: ({ children }) => <h4>{children}</h4>,
                pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
                a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
                img: ({ alt }) => <span className={styles.muted}>图片：{alt || '未自动加载'}</span>,
            }}>{children}</ReactMarkdown>}</div>;
}
function printable(value: unknown): string {
    if (value === null || value === undefined || value === '')
        return '—';
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean')
        return String(value);
    try {
        return JSON.stringify(value, null, 2) ?? '—';
    }
    catch {
        return '—';
    }
}
function GoalBody({ goal }: {
    goal: string;
}) {
    const [expanded, setExpanded] = useState(false);
    const long = goal.length > 280 || goal.split('\n').length > 5;
    return <>
    {long && !expanded ? <p className={styles.goalPreview}>{goal.slice(0, 280)}…</p> : <div className={long ? styles.goalFull : undefined}><MarkdownBody sourceToggle>{goal}</MarkdownBody></div>}
    {long && <Button className={`${controls.button} ${controls.quiet}`} onClick={() => setExpanded(!expanded)} aria-expanded={expanded} htmlType={"button"} type={"text"}>{expanded ? '收起目标全文' : '展开目标全文'}</Button>}
  </>;
}
function Meta({ label, value }: {
    label: string;
    value: unknown;
}) {
    const numbers = useContext(AgentNumbers);
    return <div className={styles.meta}><dt>{label}</dt><dd>{typeof value === 'string' && numbers[value] ? agentLabel(value, numbers) : printable(value)}</dd></div>;
}
function ObjectLinks({ ids, state, onSelect }: {
    ids: string[];
    state: BoardState;
    onSelect: Props['onSelect'];
}) {
    const numbers = useContext(AgentNumbers);
    const unique = [...new Set(ids)];
    if (!unique.length)
        return <p className={styles.muted}>暂无记录。</p>;
    return <ul className={styles.related}>{unique.map((id) => {
            const object = state.facts[id] ?? state.intents[id] ?? state.agents[id];
            const label = state.facts[id]?.statement ?? state.intents[id]?.statement
                ?? (state.agents[id] ? `Agent · ${state.agents[id].taskType}` : '当前快照中不可用');
            return <li key={id}>{object ? <Button className={styles.objectLink} onClick={() => onSelect(id)} htmlType={"button"}><strong>{state.agents[id] ? agentLabel(id, numbers) : id}</strong><span>{label}</span></Button> : <span className={styles.unavailable}>{id} · {label}</span>}</li>;
        })}</ul>;
}
function Authors({ ids, state, onSelect }: { ids: string[]; state: BoardState; onSelect: Props['onSelect'] }) {
  const numbers = useContext(AgentNumbers);
  return <div className={styles.members}>{[...new Set(ids.filter(Boolean))].map(id => <Button key={id} size="small" disabled={!state.agents[id]} onClick={() => onSelect(id)}>{agentLabel(id, numbers)}</Button>)}</div>;
}
function FactDetail({ fact, state, onSelect }: { fact: BoardFact; state: BoardState; onSelect: Props['onSelect'] }) {
  const chain = disputeChain(state, fact.id);
  const derivedBy = Object.values(state.facts).filter(item => item.derivedFrom.includes(fact.id)).map(item => item.id);
  const basedOn = Object.values(state.intents).filter(item => item.basedOn.includes(fact.id)).map(item => item.id);
  const criteria = Object.values(state.acceptance).filter(item => fact.satisfies.includes(item.id) || item.evidence_facts.includes(fact.id));
  const overview = <>
    <Authors ids={[fact.author]} state={state} onSelect={onSelect} />
    <MarkdownBody key={fact.id} sourceToggle>{fact.statement}</MarkdownBody>
    <div className={styles.compactMeta}><span>{fact.provenance === 'tool_backed' ? '工具记录支持' : 'Agent 自述'}</span><span>v{fact.version}</span><span>被依赖 {fact.reliedBy} 次</span></div>
    {fact.result && <section className={styles.section}><h3>工作结论</h3><p>{resultLabels[fact.result]}</p>{fact.resolves && <ObjectLinks ids={[fact.resolves]} state={state} onSelect={onSelect} />}</section>}
    {fact.status === 'disputed' && <section className={styles.section}><h3>争议链</h3><ObjectLinks ids={chain.map(item => item.id)} state={state} onSelect={onSelect} /></section>}
    {criteria.length > 0 && <section className={styles.section}><h3>验收关系</h3><ul className={styles.acceptance}>{criteria.map(item => <li key={item.id}><strong>{item.id} · {item.desc}</strong><span className={`${controls.badge} ${item.status === 'met' ? controls.badgeSuccess : controls.badgeWarning}`}>{item.status === 'met' ? 'Close 已裁定满足' : '尚未裁定满足'}</span><small>{fact.satisfies.includes(item.id) ? '本事实声明支持此条件' : '裁定引用此事实'}</small>{item.reason && <small>{item.reason}</small>}</li>)}</ul></section>}
  </>;
  const relations = <><section className={styles.section}><h3>事实关联</h3><p className={styles.relationLabel}>来源事实</p><ObjectLinks ids={fact.derivedFrom} state={state} onSelect={onSelect} /><p className={styles.relationLabel}>后续引用</p><ObjectLinks ids={[...derivedBy, ...basedOn]} state={state} onSelect={onSelect} />{fact.resolves && <><p className={styles.relationLabel}>关闭的意图</p><ObjectLinks ids={[fact.resolves]} state={state} onSelect={onSelect} /></>}</section>{chain.length > 1 && <section className={styles.section}><h3>争议链</h3><ObjectLinks ids={chain.map(item => item.id)} state={state} onSelect={onSelect} /></section>}</>;
  return <><div className={styles.objectHeading}><h2 tabIndex={-1}>{factKinds[fact.kind] ?? '事实'} · {fact.id}</h2><BlackboardState kind="fact" status={fact.status} /></div><Tabs className={styles.objectTabs} items={[{ key:'overview', label:'概览', children:overview }, { key:'relations', label:'关联', children:relations }, { key:'evidence', label:`证据 ${fact.evidence.length}`, children:<section className={styles.section}><h3>证据</h3>{fact.evidence.length ? <ul className={styles.evidence}>{fact.evidence.map((item,index)=><li key={index}><EvidenceViewer evidence={item} /></li>)}</ul> : <p className={styles.muted}>没有登记证据。</p>}</section> }]} /></>;
}
function IntentDetail({ intent, state, onSelect }: { intent: BoardIntent; state: BoardState; onSelect: Props['onSelect'] }) {
  const chain = retryChain(state, intent.id);
  const overview = <><Authors ids={[intent.author, intent.holder ?? '']} state={state} onSelect={onSelect} /><MarkdownBody key={intent.id} sourceToggle>{intent.statement}</MarkdownBody><div className={styles.compactMeta}><span>v{intent.version}</span><span>尝试 {intent.attempts} 次</span></div><section className={styles.section}><h3>预期结果</h3><MarkdownBody>{intent.expected}</MarkdownBody></section><section className={styles.section}><h3>调查方法</h3><MarkdownBody>{intent.method}</MarkdownBody></section>{intent.result && <section className={styles.section}><h3>工作结论</h3><p>{resultLabels[intent.result]}</p><ObjectLinks ids={intent.resultFacts} state={state} onSelect={onSelect} /></section>}</>;
  const records = <section className={styles.section}><h3>执行记录</h3>{intent.notes?.length ? <ol className={styles.notes}>{intent.notes.map((note,index)=><li key={`${note.at}-${index}`}><small>{note.by} · <time dateTime={note.at}>{note.at}</time></small><MarkdownBody>{note.text}</MarkdownBody></li>)}</ol> : <p className={styles.muted}>没有登记执行记录。</p>}{chain.length > 1 && <><h3 className={styles.followingHeading}>重试链</h3><ObjectLinks ids={chain.map(item=>item.id)} state={state} onSelect={onSelect} /></>}</section>;
  const relations = <section className={styles.section}><h3>关联对象</h3><p className={styles.relationLabel}>依据事实</p><ObjectLinks ids={intent.basedOn} state={state} onSelect={onSelect} /><p className={styles.relationLabel}>相关对象</p><ObjectLinks ids={intent.relatesTo} state={state} onSelect={onSelect} /><p className={styles.relationLabel}>结论事实</p><ObjectLinks ids={intent.resultFacts} state={state} onSelect={onSelect} /></section>;
  return <><div className={styles.objectHeading}><h2 tabIndex={-1}>意图 · {intent.id}</h2><BlackboardState kind="intent" status={intent.status} result={intent.result} /></div><Tabs className={styles.objectTabs} items={[{key:'overview',label:'概览',children:overview},{key:'records',label:'执行记录',children:records},{key:'relations',label:'关联',children:relations}]} /></>;
}
function AgentDetail({ agent, state, onSelect }: {
    agent: BoardAgent;
    state: BoardState;
    onSelect: Props['onSelect'];
}) {
    const currency = useContext(TaskCurrency);
    const numbers = useContext(AgentNumbers);
    const calls = Object.values(state.toolCalls ?? {}).filter((call) => call.agentId === agent.id).sort((a, b) => a.version - b.version);
    return <>
    <div className={styles.titleBlock}><span className={styles.type}>Agent · {agent.taskType}{agent.isSeed ? ' · 种子' : ''}{agent.closeMode ? ` · ${agent.closeMode}` : ''}</span><h2 tabIndex={-1}>{agentLabel(agent.id, numbers)}</h2><span className={`${controls.badge} ${agent.status === 'failed' ? controls.badgeDanger : agent.status === 'running' ? controls.badgeInfo : ''}`}>{agentStatus[agent.status] ?? agent.status}</span></div>
    <section className={styles.section}><h3>运行情况</h3><dl className={styles.metaList}><Meta label="模型调用" value={agent.steps}/><Meta label="上下文 token" value={agent.contextTokens}/><Meta label="最后版本" value={agent.lastSeenVersion}/><Meta label="结束原因" value={agent.endReason}/><Meta label="收尾原因" value={agent.concludeReason}/><Meta label="开始时间" value={agent.startedAt}/><Meta label="结束时间" value={agent.finishedAt}/></dl></section>
    {agent.intentId && <section className={styles.section}><h3>当前意图</h3><ObjectLinks ids={[agent.intentId]} state={state} onSelect={onSelect}/></section>}
    <section className={styles.section}><h3>用量与费用</h3>{Object.keys(agent.usage).length ? <dl className={styles.metaList}>{Object.entries(agent.usage).map(([key, value]) => <Meta key={key} label={key === 'cost' ? '估算费用' : key.replaceAll('_', ' ')} value={key === 'cost' ? formatMoney(value, currency) : value}/>)}</dl> : <p className={styles.muted}>暂无用量记录。</p>}</section>
    <section className={styles.section}><h3>结束回执</h3>{agent.receipt ? <pre className={styles.receipt}>{printable(agent.receipt)}</pre> : <p className={styles.muted}>暂无回执。</p>}</section>
    <section className={styles.section}><h3>工具调用 · {calls.length}</h3>{calls.length ? <ol className={styles.calls}>{calls.map((call) => <li key={call.id}><div className={styles.callHeading}><strong>{call.tool}</strong><span>{call.id} · v{call.version}</span></div><small><time dateTime={call.createdAt}>{call.createdAt}</time></small><Collapse className={styles.callDetails} size={"small"} ghost items={[{ key: "content", label: <>查看参数和结果摘要</>, children: <><pre>{printable(call.args)}</pre><pre>{call.resultHead}</pre></> }]}/>{call.resultUri && <EvidenceViewer evidence={{ type: 'command_output', summary: `${call.tool} 的完整结果`, uri: call.resultUri, call_id: call.id }}/>}</li>)}</ol> : <p className={styles.muted}>暂无工具调用。</p>}</section>
  </>;
}
export default function DetailPanel({ taskId, state, selectedId, onSelect, historical = false, agentNumbers = {} }: Props) {
    const panel = useRef<HTMLElement>(null);
    const body = useRef<HTMLDivElement>(null);
    useEffect(() => { if (selectedId)
        body.current?.querySelector('h2')?.focus(); }, [selectedId]);
    const fact = selectedId ? state.facts[selectedId] : undefined;
    const intent = selectedId ? state.intents[selectedId] : undefined;
    const agent = selectedId ? state.agents[selectedId] : undefined;
    function close() {
        const node = selectedId ? document.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(selectedId)}"] button`) : null;
        onSelect(null);
        requestAnimationFrame(() => { if (node?.isConnected)
            node.focus({ preventScroll: true });
        else
            document.querySelector<HTMLElement>('[aria-label="黑板关系图"]')?.focus({ preventScroll: true }); });
    }
    return <AgentNumbers.Provider value={agentNumbers}><aside key={taskId} ref={panel} tabIndex={-1} className={styles.panel} aria-label={historical ? '历史快照详情' : '对象详情'} onKeyDown={(event) => { if (event.key === 'Escape' && selectedId)
        close(); }}>
    <div className={styles.top}><span className={styles.panelLabel}>详情{historical ? ' · 历史快照' : ''}</span>{selectedId && <InspectorActions onClose={close} />}</div>
    {!selectedId ? <div className={styles.placeholder}><h2>选择图上的节点</h2><p>查看任务目标、事实、意图或 Agent 的详细信息。</p></div> :
            <div ref={body} className={styles.body}>{selectedId === 'goal' ? <>
        <div className={styles.titleBlock}><h2 tabIndex={-1}>{state.task?.name || '任务目标'}</h2><span className={controls.badge}>{state.task?.status ?? '尚未建立'}</span></div><GoalBody goal={state.task?.goal ?? ''}/>
        {Boolean(state.task?.initialAttachments?.length) && <section className={styles.section}><h3>初始附件</h3><ul className={styles.related}>{state.task?.initialAttachments?.map((file) => <li key={file.id}><a href={api.evidenceUrl(file.uri)} download>{file.filename}</a><small>{file.path}</small></li>)}</ul></section>}
        <section className={styles.section}><h3>任务状态</h3><dl className={styles.metaList}><Meta label="结束原因" value={state.task?.fail_reason ?? state.task?.closingReason}/><Meta label="开始时间" value={state.task?.startedAt}/><Meta label="结束时间" value={state.task?.finishedAt}/><Meta label="版本" value={state.task?.version}/></dl></section>
        {state.task?.domain_context && <section className={styles.section}><h3>领域背景</h3><MarkdownBody>{state.task.domain_context}</MarkdownBody></section>}
        <section className={styles.section}><h3>验收条件</h3><ul className={styles.acceptance}>{(state.task?.acceptance ?? []).map(({ id, desc }) => { const item = state.acceptance[id]; return <li key={id}><strong>{id} · {desc}</strong><span className={`${controls.badge} ${item?.status === 'met' ? controls.badgeSuccess : controls.badgeWarning}`}>{item?.status === 'met' ? '已满足' : '未满足'}</span>{item?.reason && <small>裁定：{item.reason}</small>}{item?.missing && <small>缺口：{item.missing}</small>}{item?.status === 'met' && <small>完成依据：{item.completion_basis === 'explicit' ? '明确达成，可免推导复核' : '需结合独立复核'}{item.completion_reason ? ` · ${item.completion_reason}` : ''}</small>}{item?.evidence_facts.length ? <ObjectLinks ids={item.evidence_facts} state={state} onSelect={onSelect}/> : null}</li>; })}</ul></section>
      </> : fact ? <FactDetail fact={fact} state={state} onSelect={onSelect}/> : intent ? <IntentDetail intent={intent} state={state} onSelect={onSelect}/> : agent ? <AgentDetail agent={agent} state={state} onSelect={onSelect}/> : <div className={styles.message}><h2 tabIndex={-1}>节点已不在当前视图中</h2><p>选择其他节点继续查看。</p></div>}</div>}
  </aside></AgentNumbers.Provider>;
}

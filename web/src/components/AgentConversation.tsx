import { Alert, Avatar, Button, Collapse, Modal } from 'antd';
import { Bubble, Sender } from '@ant-design/x';
import InspectorActions from './InspectorActions';
import ReasoningBlock from './ReasoningBlock';
import ToolSteps from './ToolSteps';
import {createAgentClientState,type AgentClientState} from './agentClientState';
import { TaskCurrency } from '../pages/currency';
import { useContext, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import { api, type AgentMessage, type PreviewData } from '../api/client';
import { agentEndReasonLabel, agentRole, agentStatusLabel, taskDuration } from '../board/agents';
import { formatMoney } from '../pages/format';
import { conversationEntries, type TraceEntry } from '../board/conversation';
import type { BoardAgent, BoardEvent, BoardState, BoardToolCall } from '../board/types';
import EvidenceViewer from './EvidenceViewer';
import { readableProse } from './readable';
import styles from './AgentConversation.module.css';
type Props = {
    taskId: string;
    agent: BoardAgent;
    label: string;
    events: BoardEvent[];
    state: BoardState;
    historical: boolean;
    onClose: () => void;
    contribution?: {
        facts: string[];
        intents: string[];
        judgments: number;
    };
    color: CSSProperties;
    onSelect: (id: string) => void;
    clientState?:AgentClientState;
    draftValue?: string;
    onDraftChange?: (value:string)=>void;
};
const traceLabels = { initial_context: '初始上下文', board_update: '黑板同步注入', model_output: '模型回复', model_error: '模型请求失败' };
const messageStatus = { queued: '等待送达', processing: '处理中', delivered: '已送达', completed: '已回复', failed: '发送或回复失败' };
function MessageBody({ text }: {
    text: string;
}) {
    return <div className={styles.chatText}><ReactMarkdown skipHtml remarkPlugins={[readableProse]} components={{
            h1: ({ children }) => <h3>{children}</h3>,
            h2: ({ children }) => <h3>{children}</h3>,
            h3: ({ children }) => <h4>{children}</h4>,
            pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
            img: ({ alt }) => <span>{alt || '图片未自动加载'}</span>,
            a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
        }}>{text}</ReactMarkdown></div>;
}
function ChatMessage({ message, onRetry }: {
    message: AgentMessage;
    onRetry: (text: string) => void;
}) {
    const currency = useContext(TaskCurrency);
    return <><MessageBody text={message.content} />{message.role==='user'&&<p className={styles.recordNote}>{messageStatus[message.status]}{message.status==='failed'&&<> · {message.error||'请稍后重试'} <Button size="small" type="link" onClick={()=>onRetry(message.content)}>重新编辑</Button></>}</p>}{message.role==='assistant'&&message.usage?.cost!=null&&<p className={styles.recordNote}>本次复盘费用 {formatMoney(message.usage.cost,currency)}</p>}{message.role==='assistant'&&message.usage?.unavailable===true&&<p className={styles.recordNote}>回复已恢复，但本轮用量未能恢复。</p>}</>;
}
function formatTime(value: string) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}
function TraceBody({ entry, autoOpen=false }: {
    entry: TraceEntry;
    autoOpen?:boolean;
}) {
    const [open, setOpen] = useState(autoOpen);
    const [preview, setPreview] = useState<PreviewData | null>(null);
    const [error, setError] = useState('');
    const [retry, setRetry] = useState(0);
    useEffect(() => {
        if (!open)
            return;
        const controller = new AbortController();
        void api.getEvidencePreview(entry.uri, controller.signal).then((value) => {
            if (!controller.signal.aborted)
                setPreview(value);
        }).catch((cause: unknown) => {
            if (!controller.signal.aborted)
                setError(cause instanceof Error ? cause.message : '读取失败');
        });
        return () => controller.abort();
    }, [open, entry.uri, retry]);
    let body: {
        text: string;
        reasoning?: string;
    } | null = null;
    if (preview && !preview.binary && !preview.truncated) {
        try {
            const value: unknown = JSON.parse(preview.text);
            if (value && typeof value === 'object' && 'text' in value && typeof value.text === 'string') {
                body = { text: value.text, reasoning: 'reasoning' in value && typeof value.reasoning === 'string' ? value.reasoning : undefined };
            }
        }
        catch { /* Show an invalid-record message below. */ }
    }
    const visibleText = body?.text ?? (preview?.truncated && !preview.binary ? preview.text : null);
    async function copy() {
        if (!visibleText)
            return;
        try {
            await navigator.clipboard.writeText(visibleText);
        }
        catch {
            setError('复制失败，请选中文本后复制。');
        }
    }
    return <div className={styles.traceBody}>
    {!open && <p className={styles.entrySummary}>{entry.summary || `第 ${entry.step} 步`}</p>}
    <Button size="small" type="text" className={styles.load} aria-expanded={open} onClick={() => setOpen(!open)} htmlType={"button"}>{open ? '收起正文' : '查看正文'}</Button>
    {open && <div className={styles.loaded}>
      <div className={styles.contentActions}><a href={api.evidenceUrl(entry.uri)} download>下载完整记录</a>{visibleText && <Button onClick={() => void copy()} htmlType={"button"}>复制正文</Button>}</div>
      {!preview && !error && <p role="status">正在读取记录…</p>}
      {error && <p role="alert">{error} <Button onClick={() => { setError(''); setPreview(null); setRetry((value) => value + 1); }} htmlType={"button"}>重试</Button></p>}
      {preview?.binary && <p>记录不是可显示的文本，请下载查看。</p>}
      {preview?.truncated && <p>正文过长，仅显示文件开头和结尾。下载可查看完整记录。</p>}
      {visibleText !== null && (entry.kind === 'model_output' && body ? <>
        {visibleText.trim() ? <MessageBody text={visibleText}/> : <p className={styles.recordNote}>本轮未返回正文。</p>}
        <Collapse className={styles.rawSource} size={"small"} ghost items={[{ key: "content", label: <>原始文本</>, children: <><pre tabIndex={0}>{visibleText}</pre></> }]}/>
      </> : <pre tabIndex={0}>{visibleText}</pre>)}
      {body?.reasoning&&<ReasoningBlock title="实际返回的推理"><MessageBody text={body.reasoning} /></ReasoningBlock>}
      {body && !body.reasoning && entry.kind === 'model_output' && <p className={styles.recordNote}>本轮模型未返回推理文本。</p>}
      {preview && !preview.binary && !preview.truncated && !body && <p role="alert">记录格式无法解析，请下载原始记录查看。</p>}
    </div>}
  </div>;
}
function ToolCall({call}:{call:BoardToolCall}) {
  return <><ToolSteps steps={[{id:call.id,name:call.tool,content:<><h4>参数</h4><pre>{JSON.stringify(call.args,null,2)}</pre><h4>返回摘要</h4><pre>{call.resultHead||'无摘要'}</pre></>}]} />{call.resultUri&&<EvidenceViewer evidence={{type:'command_output',summary:`${call.tool} 的完整结果`,uri:call.resultUri,call_id:call.id}} />}</>;
}
export default function AgentConversation({ taskId, agent, label, events, state, historical, onClose, contribution, color, onSelect, draftValue, onDraftChange, clientState }: Props) {
    const currency = useContext(TaskCurrency);
    const heading = useRef<HTMLHeadingElement>(null);
    const queryClient = useQueryClient();
    const [localDraft,setLocalDraft]=useState('');
    const fallback=useRef(createAgentClientState());const client=clientState??fallback.current;
    const alive=useRef(true);useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
    const draft=draftValue??localDraft;
    const setDraft=(value:string)=>{client.draftRevision+=1;(onDraftChange??setLocalDraft)(value);};
    const [infoOpen,setInfoOpen]=useState(false);
    const [sending, setSending] = useState(false);
    const [sendError, setSendError] = useState('');
    const messages = useQuery({ queryKey: ['agent-messages', taskId, agent.id], queryFn: () => api.getAgentMessages(taskId, agent.id), refetchInterval: historical ? false : 2000 });
    const entries = useMemo(() => conversationEntries(agent.id, events, state, messages.data?.messages, historical), [agent.id, events, state, messages.data?.messages, historical]);
    const hasTrace = entries.some((entry) => entry.type === 'trace');
    const elapsed = taskDuration(agent.startedAt, agent.finishedAt, historical ? events.at(-1)?.created_at : null, Date.now());
    const review = messages.data?.mode === 'review' || !['running', 'concluding'].includes(agent.status);
    useEffect(() => { heading.current?.focus(); }, [taskId, agent.id]);
    async function send() {
        const content = draft.trim();
        if (historical || sending || !content || content.length > 20000)
            return;
        if(client.message?.content!==content)client.message={id:crypto.randomUUID(),content};
        const outbound=client.message;const revision=client.draftRevision;
        setSending(true);
        setSendError('');
        try {
            await api.sendAgentMessage(taskId,agent.id,outbound);
            if(client.message===outbound)client.message=null;
            if(client.draftRevision===revision&&(onDraftChange||alive.current))setDraft('');
            await queryClient.invalidateQueries({ queryKey: ['agent-messages', taskId, agent.id] });
        }
        catch (error) {
            if(alive.current)setSendError(error instanceof Error?error.message:'发送失败，请重试。');
        }
        finally {
            if(alive.current)setSending(false);
        }
    }
    const autoExpanded = new Set(entries.filter(entry=>entry.type==='trace'&&entry.kind==='model_output').slice(-6).map(entry=>entry.type==='trace'?entry.version:0));
    return <aside className={styles.panel} style={color} aria-label={`${label} 对话记录`}>
      <header className={styles.header}><Avatar size={28} shape="square" style={{background:'var(--agent-tint,#eaf0fb)',color:'var(--agent-accent,#386ac0)'}}>{label.split(' ').at(-1)}</Avatar><div className={styles.identity}><h2 tabIndex={-1} ref={heading} title={agent.id}>{label}<small>{agentRole(agent)}{historical?' · 历史快照':''}</small></h2><p>{agentStatusLabel[agent.status]} · 模型调用 {agent.steps} 次</p></div><InspectorActions onClose={onClose} closeLabel="关闭对话" menu={{items:[{key:'info',label:'运行信息'}],onClick:()=>setInfoOpen(true)}} /></header>
      {!hasTrace&&<p className={styles.empty}>{agent.status==='running'||agent.status==='concluding'?'对话记录尚未到达；工具调用会在此显示。':'此任务未记录 Agent 上下文与模型回复。已有的工具调用和结束回执仍可查看。'}</p>}
      <Bubble.List className={styles.messages} autoScroll={!historical} role={{assistant:{variant:'borderless',styles:{root:{paddingBlock:6,paddingInlineEnd:0},content:{padding:0,minHeight:0},body:{gap:4},header:{marginBottom:2}}},user:{placement:'end',styles:{root:{paddingBlock:6,paddingInlineStart:'12%'},content:{padding:'7px 10px',minHeight:0},header:{marginBottom:2}}}}} items={entries.map(entry=>({
        key:entry.type==='chat'?entry.message.id:`${entry.type}-${entry.version}`,role:entry.type==='chat'&&entry.message.role==='user'?'user':'assistant',typing:false,streaming:false,
        header:<div className={styles.entryHead}><strong>{entry.type==='chat'?entry.message.role==='user'?'你':'Agent 回复':entry.type==='tool'?'工具调用':traceLabels[entry.kind]+(entry.kind==='initial_context'&&entry.deriveRound?` · 第 ${entry.deriveRound} 轮`:'')}</strong><span>{entry.type!=='chat'?`v${entry.version} · `:''}{formatTime(entry.type==='chat'?entry.message.created_at:entry.type==='tool'?entry.call.createdAt:entry.at)}</span></div>,
        content:<article aria-label={`${entry.type==='chat'?entry.message.role==='user'?'用户消息':'Agent回复':entry.type==='tool'?'工具调用':traceLabels[entry.kind]} ${entry.type==='chat'?entry.message.id:'v'+entry.version}`}>{entry.type==='chat'?<ChatMessage message={entry.message} onRetry={text=>{setDraft(text);setSendError('');}} />:entry.type==='tool'?<ToolCall call={entry.call} />:<TraceBody entry={entry} autoOpen={autoExpanded.has(entry.version)} />}</article>,
      }))} />
      <div className={styles.composer}><Sender aria-label={historical?'历史回放 · 只读':review?'继续对话 · 只读复盘':`对 ${label} 说`} value={draft} onChange={value=>setDraft(value.slice(0,20000))} disabled={historical||sending} autoSize={{minRows:1,maxRows:4}} styles={{content:{padding:'8px 10px',gap:6},input:{fontSize:14,lineHeight:1.5}}} placeholder={historical?'返回实时视图后可发送消息':review?'询问结论、证据或下一步建议…':'补充信息、纠正方向或提出问题…'} onSubmit={()=>void send()} onKeyDown={event=>{if(event.key==='Enter'){if(event.ctrlKey||event.metaKey){event.preventDefault();void send();}return false;}}} suffix={(_, {components})=><components.SendButton aria-label={sendError?'重试发送':'发送'} loading={sending} disabled={historical||sending||!draft.trim()||messages.isLoading} />} />
        {!historical&&review&&messages.data?.session_available===false&&<p className={styles.recordNote}>旧会话未保存，将基于已有黑板和记录开始复盘。</p>}{!historical&&review&&messages.data?.session_available&&messages.data.session_origin==='legacy'&&<p className={styles.recordNote}>此复盘会话基于旧任务记录初始化。</p>}
        {(sendError||messages.error)&&<Alert type="error" title={sendError||messages.error?.message} />}
        <div className={styles.composerHint}><span>{historical?'回放期间不可发送':review?'只读复盘 · 费用单独记录':'下一轮模型调用时送达'}</span><span>Ctrl/⌘+Enter</span></div>
      </div>
      <Modal title={`${label} · 运行信息`} open={infoOpen} onCancel={()=>setInfoOpen(false)} footer={null}><dl className={styles.metrics}><dt>持续时间</dt><dd>{elapsed}</dd><dt>模型调用</dt><dd>{agent.steps}</dd><dt>当前上下文</dt><dd>{agent.contextTokens.toLocaleString()} token</dd><dt>输出 / 推理</dt><dd>{(agent.usage.output_tokens??0).toLocaleString()} / {(agent.usage.reasoning_tokens??0).toLocaleString()} token</dd><dt>估算费用</dt><dd>{formatMoney(agent.usage.cost,currency)}</dd><dt>会话</dt><dd>{messages.data?.session_available?'已保存':'未保存原生会话'}</dd></dl>{contribution&&<section><h3>产出 · {contribution.facts.length} Fact · {contribution.intents.length} Intent</h3><div className={styles.contribution}>{[...contribution.facts,...contribution.intents].map(id=><Button size="small" key={id} onClick={()=>{setInfoOpen(false);onSelect(id);}}>{id}</Button>)}</div>{contribution.judgments>0&&<p>裁定 {contribution.judgments} 次</p>}</section>}{agent.endReason&&<p>结束原因：{agentEndReasonLabel[agent.endReason]??agent.endReason}</p>}{agent.receipt!=null&&<Collapse ghost size="small" items={[{key:'receipt',label:'探索结束回执',children:<pre>{typeof agent.receipt==='string'?agent.receipt:JSON.stringify(agent.receipt,null,2)}</pre>}]} />}</Modal>
    </aside>;
}

import { TaskCurrency } from '../pages/currency';
import { useContext, useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import { api, type AgentMessage, type PreviewData } from '../api/client';
import { agentEndReasonLabel, agentRole, agentStatusLabel, taskDuration } from '../board/agents';
import { formatMoney } from '../pages/format';
import { conversationEntries, type TraceEntry } from '../board/conversation';
import type { BoardAgent, BoardEvent, BoardState, BoardToolCall } from '../board/types';
import EvidenceViewer from './EvidenceViewer';
import styles from './AgentConversation.module.css';

type Props = {
  taskId: string; agent: BoardAgent; label: string; events: BoardEvent[]; state: BoardState;
  historical: boolean; onClose: () => void;
  contribution?: { facts: string[]; intents: string[]; judgments: number };
  color: CSSProperties;
  onSelect: (id: string) => void;
};

const traceLabels = { initial_context: '初始上下文', board_update: '黑板同步注入', model_output: '模型回复' };
const messageStatus = { queued: '等待送达', processing: '处理中', delivered: '已送达', completed: '已回复', failed: '发送或回复失败' };

function ChatMessage({ message, onRetry }: { message: AgentMessage; onRetry: (text: string) => void }) {
  const currency = useContext(TaskCurrency);
  return <article className={`${styles.entry} ${message.role === 'user' ? styles.userMessage : styles.assistantMessage}`}>
    <div className={styles.entryHead}><strong>{message.role === 'user' ? '你' : 'Agent 回复'}</strong><time dateTime={message.created_at}>{formatTime(message.created_at)}</time></div>
    <div className={styles.chatText}><ReactMarkdown components={{ img: ({ alt }) => <span>{alt || '图片未加载'}</span>, a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{message.content}</ReactMarkdown></div>
    {message.role === 'user' && <p className={styles.recordNote}>{messageStatus[message.status]}{message.status === 'failed' && <> · {message.error || '请稍后重试'} <button type="button" onClick={() => onRetry(message.content)}>重新编辑</button></>}</p>}
    {message.role === 'assistant' && message.usage?.cost != null && <p className={styles.recordNote}>本次复盘费用 {formatMoney(message.usage.cost, currency)}</p>}
    {message.role === 'assistant' && message.usage?.unavailable === true && <p className={styles.recordNote}>回复已恢复，但本轮用量未能恢复。</p>}
  </article>;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}

function TraceBody({ entry }: { entry: TraceEntry }) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void api.getEvidencePreview(entry.uri, controller.signal).then((value) => {
      if (!controller.signal.aborted) setPreview(value);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '读取失败');
    });
    return () => controller.abort();
  }, [open, entry.uri, retry]);

  let body: { text: string; reasoning?: string } | null = null;
  if (preview && !preview.binary && !preview.truncated) {
    try {
      const value: unknown = JSON.parse(preview.text);
      if (value && typeof value === 'object' && 'text' in value && typeof value.text === 'string') {
        body = { text: value.text, reasoning: 'reasoning' in value && typeof value.reasoning === 'string' ? value.reasoning : undefined };
      }
    } catch { /* Show an invalid-record message below. */ }
  }
  const visibleText = body?.text ?? (preview?.truncated && !preview.binary ? preview.text : null);
  async function copy() {
    if (!visibleText) return;
    try { await navigator.clipboard.writeText(visibleText); } catch { setError('复制失败，请选中文本后复制。'); }
  }
  return <div className={styles.traceBody}>
    <button type="button" className={styles.load} aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起正文' : '查看正文'}</button>
    {open && <div className={styles.loaded}>
      <div className={styles.contentActions}><a href={api.evidenceUrl(entry.uri)} download>下载完整记录</a>{visibleText && <button type="button" onClick={() => void copy()}>复制正文</button>}</div>
      {!preview && !error && <p role="status">正在读取记录…</p>}
      {error && <p role="alert">{error} <button type="button" onClick={() => { setError(''); setPreview(null); setRetry((value) => value + 1); }}>重试</button></p>}
      {preview?.binary && <p>记录不是可显示的文本，请下载查看。</p>}
      {preview?.truncated && <p>正文过长，仅显示文件开头和结尾。下载可查看完整记录。</p>}
      {visibleText !== null && <pre>{visibleText}</pre>}
      {body?.reasoning && <details className={styles.reasoning}><summary>实际返回的推理</summary><pre>{body.reasoning}</pre></details>}
      {body && !body.reasoning && entry.kind === 'model_output' && <p className={styles.recordNote}>本轮模型未返回推理文本。</p>}
      {preview && !preview.binary && !preview.truncated && !body && <p role="alert">记录格式无法解析，请下载原始记录查看。</p>}
    </div>}
  </div>;
}

function ToolCall({ call }: { call: BoardToolCall }) {
  return <article className={`${styles.entry} ${styles.tool}`}>
    <div className={styles.entryHead}><span className={styles.kind}>工具调用</span><span>v{call.version} · <time dateTime={call.createdAt}>{formatTime(call.createdAt)}</time></span></div>
    <strong className={styles.toolName}>{call.tool}</strong>
    <details className={styles.toolDetails}><summary>参数与结果摘要</summary><h4>参数</h4><pre>{JSON.stringify(call.args, null, 2)}</pre><h4>返回摘要</h4><pre>{call.resultHead || '无摘要'}</pre></details>
    {call.resultUri && <EvidenceViewer evidence={{ type: 'command_output', summary: `${call.tool} 的完整结果`, uri: call.resultUri, call_id: call.id }} />}
  </article>;
}

export default function AgentConversation({ taskId, agent, label, events, state, historical, onClose, contribution, color, onSelect }: Props) {
  const currency = useContext(TaskCurrency);
  const heading = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState('');
  const request = useRef<{ id: string; content: string } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const messages = useQuery({ queryKey: ['agent-messages', taskId, agent.id], queryFn: () => api.getAgentMessages(taskId, agent.id), refetchInterval: historical ? false : 2000 });
  const entries = useMemo(() => conversationEntries(agent.id, events, state, messages.data?.messages, historical), [agent.id, events, state, messages.data?.messages, historical]);
  const hasTrace = entries.some((entry) => entry.type === 'trace');
  const elapsed = taskDuration(agent.startedAt, agent.finishedAt, historical ? events.at(-1)?.created_at : null, Date.now());
  const review = messages.data?.mode === 'review' || !['running', 'concluding'].includes(agent.status);
  useEffect(() => { heading.current?.focus(); }, [taskId, agent.id]);
  useEffect(() => { if (following.current && !historical) end.current?.scrollIntoView({ block: 'nearest' }); }, [entries.length, historical]);
  async function send(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (historical || sending || !content || content.length > 20000) return;
    if (request.current?.content !== content) request.current = { id: crypto.randomUUID(), content };
    setSending(true); setSendError('');
    try {
      await api.sendAgentMessage(taskId, agent.id, request.current);
      request.current = null; setDraft(''); following.current = true;
      await queryClient.invalidateQueries({ queryKey: ['agent-messages', taskId, agent.id] });
    } catch (error) { setSendError(error instanceof Error ? error.message : '发送失败，请重试。'); }
    finally { setSending(false); }
  }
  return <aside className={styles.panel} style={color} aria-label={`${label} 对话记录`} onKeyDown={(event) => { if (event.key === 'Escape') onClose(); }}>
    <header className={styles.header}>
      <div><span className={styles.overline}>Agent 对话{historical ? ' · 历史快照' : ''}</span><h2 tabIndex={-1} ref={heading} title={agent.id}>{label} <span>· {agentRole(agent)}</span></h2><p>{agentStatusLabel[agent.status]} · 模型调用 {agent.steps} 次{!historical && messages.data?.session_available && messages.data.session_origin === 'native' ? ' · 会话已保存' : ''}</p></div>
      <button type="button" className={styles.close} onClick={onClose} aria-label="关闭对话">×</button>
    </header>
    <div className={styles.scroll} ref={scroll} onScroll={() => { const node = scroll.current; if (node) following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80; }}>
      <details className={styles.metrics}><summary>运行概况 · {elapsed}</summary><dl><dt>模型调用</dt><dd>{agent.steps} 次</dd><dt>当前上下文</dt><dd>{agent.contextTokens.toLocaleString()} token</dd><dt>输出 / 推理</dt><dd>{(agent.usage.output_tokens ?? 0).toLocaleString()} / {(agent.usage.reasoning_tokens ?? 0).toLocaleString()} token</dd><dt>估算费用</dt><dd>{formatMoney(agent.usage.cost, currency)}</dd></dl></details>
      {contribution && <details className={styles.contribution}><summary>产出 · {contribution.facts.length} Fact · {contribution.intents.length} Intent{contribution.judgments ? ` · ${contribution.judgments} 次裁定` : ''}</summary><div>{[...contribution.facts, ...contribution.intents].map((id) => <button key={id} type="button" onClick={() => onSelect(id)}>{id}</button>)}{!contribution.facts.length && !contribution.intents.length && <p>此 Agent 暂无创建的事实或意图。</p>}</div></details>}
      {!hasTrace && <p className={styles.empty}>{agent.status === 'running' || agent.status === 'concluding' ? '对话记录尚未到达；工具调用会在此显示。' : '此任务未记录 Agent 上下文与模型回复。已有的工具调用和结束回执仍可查看。'}</p>}
      {agent.receipt != null && <details className={styles.receipt}><summary>探索结束回执</summary><pre>{typeof agent.receipt === 'string' ? agent.receipt : JSON.stringify(agent.receipt, null, 2)}</pre></details>}
      {agent.endReason && <p className={styles.endReason}>结束原因：{agentEndReasonLabel[agent.endReason] ?? agent.endReason}</p>}
      {entries.length > 0 && <ol className={styles.entries}>{entries.map((entry) => <li key={entry.type === 'chat' ? entry.message.id : `${entry.type}-${entry.version}`}>{entry.type === 'chat' ? <ChatMessage message={entry.message} onRetry={(text) => { setDraft(text); setSendError(''); }} /> : entry.type === 'tool' ? <ToolCall call={entry.call} /> :
        <article className={styles.entry}>
          <div className={styles.entryHead}><span className={styles.kind}>{traceLabels[entry.kind]}</span><span>v{entry.version} · <time dateTime={entry.at}>{formatTime(entry.at)}</time></span></div>
          <p className={styles.entrySummary}>{entry.summary || `第 ${entry.step} 步`}</p>
          <TraceBody entry={entry} />
        </article>}</li>)}</ol>}
      <div ref={end} />
    </div>
    <form className={styles.composer} onSubmit={(event) => void send(event)}>
      <label htmlFor={`agent-message-${agent.id}`}>{historical ? '历史回放 · 只读' : review ? '继续对话 · 只读复盘' : `对 ${label} 说`}</label>
      {!historical && review && messages.data?.session_available === false && <p>旧会话未保存，将基于已有黑板和记录开始复盘。</p>}
      {!historical && review && messages.data?.session_available && messages.data.session_origin === 'legacy' && <p>此复盘会话基于旧任务记录初始化。</p>}
      <textarea id={`agent-message-${agent.id}`} rows={3} maxLength={20000} value={draft} disabled={historical || sending} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={historical ? '返回实时视图后可发送消息' : review ? '询问结论、证据或下一步建议…' : '补充信息、纠正方向或提出问题…'} />
      {(sendError || messages.error) && <p role="alert">{sendError || messages.error?.message}</p>}
      <div><span>{historical ? '回放期间不可发送' : review ? '可读取黑板与证据，费用单独记录' : '下一轮模型调用时送达'} · Ctrl/⌘+Enter</span><button type="submit" disabled={historical || sending || !draft.trim() || messages.isLoading}>{sending ? '发送中…' : '发送'}</button></div>
    </form>
  </aside>;
}

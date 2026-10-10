import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ctfApi } from './api';
import type { CtfMember } from './types';
import { canSend, conversationEntries, deliveryLabel, isReviewPhase, memberStatus } from './view';
import { useAction } from './useAction';
import Markdown from './Markdown';
import Icon from '../components/Icon';
import controls from '../styles/controls.module.css';
import styles from './CtfWorkbench.module.css';

export default function Conversation({ taskId, member, members, phase, onClose }: { taskId: string; member: CtfMember; members: CtfMember[]; phase: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const review = isReviewPhase(phase);
  const [draft, setDraft] = useState('');
  const [removing, setRemoving] = useState(false);
  const [operation, setOperation] = useState('');
  const sendAction = useAction();
  const memberAction = useAction();
  const outgoing = useRef<{ id: string; content: string } | null>(null);
  const actionIds = useRef<Record<string, string>>({});
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const messages = useQuery({ queryKey: ['ctf', taskId, 'messages', member.id], queryFn: ({ signal }) => ctfApi.messages(taskId, member.id, signal), refetchInterval: 3000 });
  const session = useQuery({ queryKey: ['ctf', taskId, 'session', member.id], queryFn: ({ signal }) => ctfApi.session(taskId, member.id, signal), refetchInterval: 3000 });
  const entries = useMemo(() => conversationEntries(session.data, messages.data?.messages ?? [], member.id), [session.data, messages.data, member.id]);
  const label = (id: string) => id === 'user' ? '你' : id === 'system' ? '系统通知' : members.find((item) => item.id === id)?.display_name ?? id;
  useEffect(() => { if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [entries]);
  async function send(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || !canSend(phase, member)) return;
    if (outgoing.current?.content !== content) outgoing.current = { id: crypto.randomUUID(), content };
    const message = outgoing.current;
    if (await sendAction.run(async (signal) => {
      await ctfApi.send(taskId, member.id, message, signal);
      await queryClient.invalidateQueries({ queryKey: ['ctf', taskId, 'messages', member.id] });
    })) { setDraft(''); outgoing.current = null; following.current = true; }
  }
  async function manage(next: 'stop' | 'resume' | 'remove') {
    setOperation(next);
    const id = actionIds.current[next] ??= crypto.randomUUID();
    if (await memberAction.run(async (signal) => {
      await ctfApi.member(taskId, member.id, next, id, signal);
      await queryClient.invalidateQueries({ queryKey: ['ctf', taskId] });
    })) { delete actionIds.current[next]; setRemoving(false); }
  }
  const error = messages.error ?? session.error;
  return <section className={styles.conversation} aria-label={`${member.display_name} 会话`}>
    <header className={styles.panelHeader}>
      <span className={styles.avatar} data-lead={member.role === 'lead'}>{member.role === 'lead' ? 'L' : member.display_name.slice(0, 1).toUpperCase()}</span>
      <div className={styles.panelIdentity}><h2 tabIndex={-1}>{member.display_name}</h2><p>{review ? '本轮已结束 · 复盘问答' : memberStatus(member)}</p></div>
      {member.role !== 'lead' && member.lifecycle !== 'removed' && phase === 'running' && <div className={styles.memberActions}>
        <button type="button" className={`${controls.button} ${controls.quiet} ${controls.compact}`} disabled={memberAction.busy || member.run_state === 'stopping'} onClick={() => void manage(member.lifecycle === 'stopped' ? 'resume' : 'stop')}>{memberAction.busy && operation !== 'remove' ? '处理中…' : member.lifecycle === 'stopped' ? '恢复成员' : '停止成员'}</button>
        <button type="button" className={`${controls.button} ${controls.quiet} ${controls.compact}`} disabled={memberAction.busy || member.run_state === 'stopping'} onClick={() => setRemoving(true)}>移除</button>
      </div>}
      <button type="button" className={`${controls.button} ${controls.quiet} ${controls.iconButton}`} onClick={onClose} aria-label="关闭详情"><Icon name="close" /></button>
    </header>
    {removing && <div className={styles.notice} role="alert"><p>移除 {member.display_name}？系统会先停止执行，会话和记录继续保留。</p><div className={styles.actions}><button type="button" className={`${controls.button} ${controls.danger}`} disabled={memberAction.busy} onClick={() => void manage('remove')}>{memberAction.busy ? '正在移除…' : '确认移除'}</button><button type="button" className={controls.button} disabled={memberAction.busy} onClick={() => setRemoving(false)}>取消</button></div></div>}
    {memberAction.error && <p className={styles.error} role="alert">{memberAction.error} 请重试相应操作。</p>}
    <div className={styles.chatScroll} ref={scroll} onScroll={() => { if (scroll.current) following.current = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 80; }}>
      {error && <div className={styles.error} role="alert">{error.message} <button type="button" onClick={() => { void messages.refetch(); void session.refetch(); }}>重新读取会话</button></div>}
      {(messages.isLoading || session.isLoading) && <p className={styles.empty} role="status">正在读取会话…</p>}
      {!entries.length && !messages.isLoading && !session.isLoading && <div className={styles.conversationEmpty}><span className={styles.largeInitial}>{member.role === 'lead' ? 'L' : member.display_name.slice(0, 1)}</span><h3>{member.role === 'lead' ? '与 Lead 开始协作' : `${member.display_name} 的会话`}</h3><p>{member.role === 'lead' ? '补充信息或调整探索方向。' : '尚无对话记录。'}</p></div>}
      {entries.map((entry) => <article key={entry.id} className={`${styles.chatEntry} ${entry.speaker === 'user' ? styles.userEntry : ''}`}>
        <div className={styles.entryMeta}><strong>{label(entry.speaker)}</strong>{entry.delivery && entry.delivery.sender_id === member.id && <span>→ {label(entry.delivery.recipient_id)}</span>}{entry.delivery?.created_at && <time dateTime={entry.delivery.created_at}>{new Date(entry.delivery.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>}</div>
        {entry.reasoning.map((reasoning, index) => <details className={styles.reasoning} key={index}><summary>{reasoning.kind === 'summary' ? '思考摘要' : '思考 · 模型返回'}</summary><div className={styles.reasoningBody}><Markdown text={reasoning.text} /></div></details>)}
        {entry.body && <Markdown text={entry.body} />}
        {entry.toolCalls.map((call) => <details className={styles.toolCall} key={call.id}><summary><Icon name="terminal" /><code>{call.name}</code><span data-status={call.status}>{call.status === 'completed' ? '已完成' : call.status === 'failed' ? '失败' : '等待结果'}</span></summary><div className={styles.toolContent}><h3>参数</h3><pre tabIndex={0}>{typeof call.arguments === 'string' ? call.arguments : JSON.stringify(call.arguments, null, 2)}</pre>{call.result !== undefined && <><h3>结果</h3><pre tabIndex={0}>{typeof call.result === 'string' ? call.result : JSON.stringify(call.result, null, 2)}</pre></>}</div></details>)}
        {entry.delivery && <small className={styles.delivery}>{deliveryLabel(entry.delivery)}</small>}
      </article>)}
    </div>
    <form className={styles.composer} onSubmit={(event) => void send(event)}>
      <label className={styles.srOnly} htmlFor={`message-${member.id}`}>发给 {member.display_name} 的消息</label>
      <textarea id={`message-${member.id}`} className={controls.textarea} rows={2} maxLength={20000} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={review ? '询问结论、证据或下一步建议…' : `发给 ${member.display_name}…`} disabled={sendAction.busy || !canSend(phase, member)} />
      <div className={styles.composerFooter}><span>{review ? '复盘问答' : phase === 'closing' ? '收尾后待处理' : member.lifecycle === 'stopped' ? '恢复后送达' : '直接发给此 Agent'}<span className={styles.shortcut}> · Ctrl/⌘+Enter</span></span><button type="submit" className={`${controls.button} ${controls.primary} ${controls.iconButton}`} aria-label={sendAction.error ? '重试发送' : review ? '发送复盘消息' : '发送消息'} disabled={sendAction.busy || !draft.trim() || !canSend(phase, member)}><Icon name={sendAction.busy ? 'more' : 'send'} /></button></div>
      {sendAction.error && <p className={styles.error} role="alert">{sendAction.error} 消息内容已保留。</p>}
    </form>
  </section>;
}

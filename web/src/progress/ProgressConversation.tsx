import { Alert, Button } from 'antd';
import { Sender } from '@ant-design/x';
import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import ConversationBubbles from '../components/ConversationBubbles';
import Markdown from '../ctf/Markdown';
import ProgressMarkdown from './ProgressMarkdown';
import { randomUUID } from '../api/uuid';
import { formatTime } from '../pages/format';
import { progressApi } from './api';
import type { ProgressNode } from './types';
import type { ProgressSendIntent } from './ProgressPage';
import styles from './ProgressPage.module.css';

export default function ProgressConversation({ taskId, node, enabled, draft, onDraftChange, onClearNode, onReference, intent, sending, onSendingChange }: {
  taskId: string; node: ProgressNode | null; enabled: boolean; draft: string; onDraftChange: (value: string) => void; onClearNode: () => void;
  onReference: (kind: 'fact' | 'intent' | 'progress', id: string, sourceVersion: number) => void;
  intent: ProgressSendIntent; sending: boolean; onSendingChange: (value: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState('');
  const query = useQuery({ queryKey: ['progress-messages', taskId], queryFn: ({ signal }) => progressApi.messages(taskId, signal), refetchInterval: current => current.state.data?.messages.some(m => m.status === 'queued' || m.status === 'processing') ? 2500 : 10000 });
  const items = useMemo(() => query.data?.messages.map(message => ({
    key: message.id, role: message.role === 'user' ? 'user' : 'assistant', typing: false, streaming: false,
    header: <div className={styles.messageMeta}><span>{message.role === 'user' ? '你' : '成果整理 Agent'}</span><time>{formatTime(message.created_at)}</time></div>,
    content: message.role === 'assistant' ? <ProgressMarkdown text={message.content} onReference={(kind, id) => onReference(kind, id, Number(message.usage?.source_version ?? 0))} /> : <Markdown text={message.content} />,
    footer: message.role === 'user' ? <span className={styles.muted}>{({ queued: '等待处理', processing: '正在整理与回复', delivered: '已送达', completed: '已回复', failed: '回复失败' })[message.status]}{message.error ? ` · ${message.error}` : ''}{message.status === 'failed' && <Button size="small" type="link" onClick={() => { onDraftChange(message.content); intent.message = null; }}>重新编辑</Button>}</span> : undefined,
  })) ?? [], [query.data, onDraftChange, onReference, intent]);
  async function send() {
    const content = draft.trim();
    if (!enabled || intent.busy || !content) return;
    intent.busy = true; onSendingChange(true); setError('');
    if (intent.message?.content !== content || intent.message.node_id !== (node?.id ?? null)) intent.message = { id: randomUUID(), content, node_id: node?.id ?? null };
    const message = intent.message, revision = intent.revision;
    try {
      await progressApi.send(taskId, message);
      if (intent.message === message) intent.message = null;
      if (intent.revision === revision) onDraftChange('');
      await queryClient.invalidateQueries({ queryKey: ['progress-messages', taskId] });
    } catch (cause) { setError(cause instanceof Error ? cause.message : '发送失败，消息已保留。'); }
    finally { intent.busy = false; onSendingChange(false); }
  }
  return <div className={styles.conversation}>
    {query.error && <Alert type="error" title={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
    <ConversationBubbles className={styles.messages} items={items} autoScroll />
    {!items.length && !query.isLoading && <p className={styles.chatEmpty}>可以询问当前认识、依据、尝试过的方向和剩余缺口。</p>}
    <div className={styles.composer}>
      {node && <div className={styles.scope}><span>围绕：{node.title}</span><Button type="text" size="small" onClick={onClearNode} aria-label="取消节点提问范围">×</Button></div>}
      <Sender aria-label="向成果整理 Agent 提问" value={draft} onChange={value => onDraftChange(value.slice(0, 20000))} disabled={!enabled || sending} submitType="enter" onSubmit={() => void send()} autoSize={{ minRows: 2, maxRows: 5 }} placeholder={enabled ? '询问现有认识、依据或尚未解决的问题…' : '启用成果整理后可提问'} onKeyDown={event => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.keyCode === 229)) return false; }} suffix={(_, { components }) => <components.SendButton loading={sending} disabled={!enabled || sending || !draft.trim()} aria-label="发送整理问题" />} />
      <div className={styles.composerHint}><span>Enter 发送 · Shift+Enter 换行</span><span>整理费用单独计量</span></div>
      {error && <Alert type="error" title={`${error} 内容已保留，可重试。`} />}
    </div>
  </div>;
}

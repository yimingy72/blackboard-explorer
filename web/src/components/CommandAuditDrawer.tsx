import { useState } from 'react';
import { Alert, Button, Drawer, Empty, Select, Tag } from 'antd';
import { CodeOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { api, type SandboxCommandSummary } from '../api/client';
import LoadingState from './LoadingState';
import styles from './CommandAuditDrawer.module.css';

type Props = {
  taskId: string;
  agents: { id: string; name: string }[];
  challenges: { id: string; title: string }[];
  open: boolean;
  onClose: () => void;
};

const statuses = {
  dispatched: { label: '已发起', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  unknown: { label: '状态未知', color: 'warning' },
};

function Status({ state }: { state: SandboxCommandSummary['state'] }) {
  const status = statuses[state] ?? statuses.unknown;
  return <Tag color={status.color}>{status.label}</Tag>;
}

function timestamp(value: string | null) {
  if (!value) return '未记录';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function CommandAudit({ taskId, agents, challenges }: Omit<Props, 'open' | 'onClose'>) {
  const [agentId, setAgentId] = useState('');
  const [cursors, setCursors] = useState<number[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const before = cursors.at(-1);
  const list = useQuery({
    queryKey: ['sandbox-commands', taskId, agentId, before],
    queryFn: ({ signal }) => api.listSandboxCommands(taskId, { agentId, before }, signal),
    refetchOnWindowFocus: false,
  });
  const detail = useQuery({
    queryKey: ['sandbox-command', taskId, selectedId],
    queryFn: ({ signal }) => api.getSandboxCommand(taskId, selectedId!, signal),
    enabled: selectedId !== null,
    refetchOnWindowFocus: false,
  });
  const command = detail.data;
  const agentName = (id: string) => agents.find(agent => agent.id === id)?.name ?? id;
  const agentOptions = new Map(agents.map(agent => [agent.id, agent.name]));
  for (const item of list.data?.items ?? []) if (!agentOptions.has(item.agent_id)) agentOptions.set(item.agent_id, item.agent_id);
  if (agentId && !agentOptions.has(agentId)) agentOptions.set(agentId, agentId);
  const refresh = () => {
    if (cursors.length) setCursors([]);
    else void list.refetch();
    if (selectedId) void detail.refetch();
  };

  return <div className={styles.audit}>
    <p className={styles.scope}>仅记录 Agent 通过平台 Sandbox 工具发起的命令，不包含旧版执行通道，且未覆盖脚本内部所有子进程。</p>
    <div className={styles.toolbar}>
      <span>Agent</span>
      <Select className={styles.agentSelect} aria-label="按 Agent 筛选命令" value={agentId} options={[{ value: '', label: '全部 Agent' }, ...Array.from(agentOptions, ([value, label]) => ({ value, label }))]} onChange={value => { setAgentId(value); setCursors([]); setSelectedId(null); }} />
      <Button onClick={refresh} loading={list.isFetching || detail.isFetching}>刷新</Button>
    </div>
    <div className={styles.workspace}>
      <section className={styles.listPane} aria-label="命令记录">
        <div className={styles.list}>
          {list.isLoading && <LoadingState label="正在读取命令记录…" />}
          {list.error && <Alert type="error" title={list.error.message} action={<Button onClick={() => void list.refetch()}>重试</Button>} />}
          {!list.isLoading && !list.error && !list.data?.items.length && <Empty description="此范围内暂无命令记录" />}
          {!list.error && <ol className={styles.entries}>{list.data?.items.map(item => <li key={item.id} data-selected={item.id === selectedId}>
            <button className={styles.entry} aria-label={`查看命令 #${item.sequence}`} aria-pressed={item.id === selectedId} onClick={() => setSelectedId(item.id)}>
              <span className={styles.entryHeader}><strong>#{item.sequence}</strong><Status state={item.state} /><span>{item.role === 'lead' ? 'Lead' : 'Worker'} · {agentName(item.agent_id)}</span></span>
              <code className={styles.commandPreview}>{item.command}</code>
              <time>{timestamp(item.created_at)}</time>
            </button>
          </li>)}</ol>}
        </div>
        <nav className={styles.pagination} aria-label="命令记录分页">
          <span>第 {cursors.length + 1} 页 · 每页 50 条</span>
          <Button size="small" disabled={!cursors.length || list.isFetching} onClick={() => { setCursors(current => current.slice(0, -1)); setSelectedId(null); }}>上一页</Button>
          <Button size="small" disabled={list.isFetching || !!list.error || list.data?.next_before == null} onClick={() => { const next = list.data?.next_before; if (next != null) { setCursors(current => [...current, next]); setSelectedId(null); } }}>下一页</Button>
        </nav>
      </section>
      <section className={styles.detail} aria-label="命令详情" aria-busy={detail.isFetching}>
        {!selectedId && <Empty description="选择一条命令查看完整记录" />}
        {selectedId && detail.isLoading && <LoadingState label="正在读取命令详情…" />}
        {selectedId && detail.error && <Alert type="error" title={detail.error.message} action={<Button onClick={() => void detail.refetch()}>重试</Button>} />}
        {command && !detail.error && <>
          <h2>命令 #{command.sequence} <Status state={command.state} /></h2>
          {command.state === 'unknown' && <Alert type="warning" showIcon title="执行结果未确认。" />}
          <h3>命令原文</h3><pre className={styles.output}>{command.command}</pre>
          <dl className={styles.metadata}>
            <dt>cwd</dt><dd><code>{command.cwd}</code></dd>
            <dt>Agent</dt><dd>{agentName(command.agent_id)}<br /><code>{command.agent_id}</code></dd>
            <dt>角色</dt><dd>{command.role === 'lead' ? 'Lead' : 'Worker'}</dd>
            <dt>题目</dt><dd>{command.challenge_id ? <>{challenges.find(item => item.id === command.challenge_id)?.title ?? '题目'}<br /><code>{command.challenge_id}</code></> : '未关联题目'}</dd>
            <dt>Assignment</dt><dd><code>{command.assignment_id || '未关联'}</code></dd>
            <dt>Generation / Epoch</dt><dd>{command.generation} / {command.epoch}</dd>
            <dt>执行 ID</dt><dd><code>{command.execution_id ?? '未确认'}</code></dd>
            <dt>记录 ID</dt><dd><code>{command.id}</code></dd>
            <dt>后台执行</dt><dd>{command.background ? '是' : '否'}</dd>
            <dt>Exit code</dt><dd>{command.exit_code ?? '未记录'}</dd>
            <dt>发起时间</dt><dd>{timestamp(command.created_at)}</dd>
            <dt>开始时间</dt><dd>{timestamp(command.started_at)}</dd>
            <dt>结束时间</dt><dd>{timestamp(command.finished_at)}</dd>
          </dl>
          {command.output_truncated && <Alert type="warning" showIcon title="输出已截断：以下仅展示已保存的部分内容，每个输出流最多保留 64 KiB。" />}
          <h3>stdout <small>已观察 {command.stdout_bytes} 字节</small></h3><pre className={styles.output}>{command.stdout || '（无已保存输出）'}</pre>
          <h3>stderr <small>已观察 {command.stderr_bytes} 字节</small></h3><pre className={styles.output}>{command.stderr || '（无已保存输出）'}</pre>
        </>}
      </section>
    </div>
  </div>;
}

export default function CommandAuditDrawer(props: Props) {
  return <Drawer title={<span className={styles.title}><CodeOutlined aria-hidden="true" />命令记录</span>} open={props.open} onClose={props.onClose} size="min(1200px, 100vw)" destroyOnHidden styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column' } }}>
    {props.open && <CommandAudit key={props.taskId} taskId={props.taskId} agents={props.agents} challenges={props.challenges} />}
  </Drawer>;
}

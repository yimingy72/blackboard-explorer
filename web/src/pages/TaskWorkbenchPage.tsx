import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { useBoard } from '../board/store';
import { reduceBoard } from '../board/reducer';
import { taskEndExplanation } from '../board/view';
import { agentContributions, agentStyle, agentLabel, agentNumbers, agentRole, agentStatusLabel, orderedAgents, taskDuration } from '../board/agents';
import AgentConversation from '../components/AgentConversation';
import WorkbenchDrawer from '../components/WorkbenchDrawer';
import DetailPanel from '../components/DetailPanel';
import TopologyFlowCanvas from '../components/TopologyFlowCanvas';
import controls from '../styles/controls.module.css';
import { formatCost, taskStatusLabel } from './format';
import styles from './TaskWorkbenchPage.module.css';

const connectionText = {
  connecting: '正在连接实时事件',
  open: '实时连接正常',
  reconnecting: '连接中断，正在重连',
  closed: '实时连接已关闭',
};

function statusClass(status: string) {
  if (status === 'running') return controls.badgeInfo;
  if (status === 'finished') return controls.badgeSuccess;
  if (status === 'failed') return controls.badgeDanger;
  if (status === 'closing' || status === 'provisioning') return controls.badgeWarning;
  return '';
}

export default function TaskWorkbenchPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [recordsOpen, setRecordsOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const agentButtons = useRef(new Map<string, HTMLButtonElement>());
  const [action, setAction] = useState<'start' | 'stop' | null>(null);
  const [actionError, setActionError] = useState('');
  const [version, setVersion] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const taskQuery = useQuery({ queryKey: ['task', taskId], queryFn: () => api.getTask(taskId!), enabled: Boolean(taskId) });
  const board = useBoard(taskId);
  const viewEvents = useMemo(() => version === null ? board.events : board.events.filter((event) => event.version <= version), [board.events, version]);
  const viewState = useMemo(() => version === null ? board.state : reduceBoard(viewEvents), [version, board.state, viewEvents]);
  const totalObjects = Object.keys(viewState.facts).length + Object.keys(viewState.intents).length;
  const unauthorized = (taskQuery.error instanceof ApiError && taskQuery.error.status === 401) || (board.error instanceof ApiError && board.error.status === 401);
  const numbers = useMemo(() => agentNumbers(viewEvents), [viewEvents]);
  const agents = useMemo(() => orderedAgents(viewState, numbers), [viewState, numbers]);
  const contributions = useMemo(() => agentContributions(viewState, viewEvents), [viewState, viewEvents]);
  const selectedAgent = selectedId ? viewState.agents[selectedId] : undefined;
  function closeAgent(id: string) {
    setSelectedId(null);
    requestAnimationFrame(() => agentButtons.current.get(id)?.focus());
  }

  useEffect(() => {
    if (version !== null || !['running', 'closing'].includes(board.state.task?.status ?? '')) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [version, board.state.task?.status]);

  useEffect(() => { setSelectedId(null); setVersion(null); setRecordsOpen(false); }, [taskId]);
  useEffect(() => {
    if (selectedId && board.state.agents[selectedId] && !viewState.agents[selectedId]) setSelectedId(null);
  }, [selectedId, board.state.agents, viewState.agents]);
  useEffect(() => {
    const focus = new URLSearchParams(location.search).get('focus');
    if (focus && /^(?:[FI]\d+|agent-\d+|goal)$/.test(focus)) setSelectedId(focus);
  }, [taskId, location.search]);
  useEffect(() => {
    if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname } });
  }, [unauthorized, navigate, location.pathname]);

  async function changeStatus(next: 'start' | 'stop') {
    if (!taskId) return;
    setAction(next);
    setActionError('');
    try {
      if (next === 'start') await api.startTask(taskId);
      else await api.stopTask(taskId);
      await queryClient.invalidateQueries({ queryKey: ['task', taskId] });
      await queryClient.invalidateQueries({ queryKey: ['tasks'] });
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : '操作失败，请重试。');
    } finally {
      setAction(null);
    }
  }

  if (!taskId) return null;
  if (board.taskId !== taskId || (taskQuery.isLoading && board.loading)) return <div className={styles.loading} role="status" aria-label="正在加载任务"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div>;
  if (taskQuery.isError && !unauthorized) return <div className={styles.errorState} role="alert"><h1>无法打开任务</h1><p>{taskQuery.error instanceof ApiError ? taskQuery.error.message : '请稍后重试。'}</p><div><button type="button" className={controls.button} onClick={() => void taskQuery.refetch()}>重试</button><Link to="/tasks" className={controls.button}>返回任务列表</Link></div></div>;

  const task = taskQuery.data;
  const boardTask = viewState.task;
  const status = boardTask?.status ?? task?.status ?? 'created';
  const usedCost = boardTask?.usage.cost ?? (version === null ? task?.usage?.cost : 0) ?? 0;
  const maxCost = boardTask?.budget.max_cost;
  const costLimit = Number(maxCost);
  const costProgress = Number.isFinite(costLimit) && costLimit > 0 ? Math.min(100, Math.max(0, Number(usedCost) / costLimit * 100)) : 0;
  const reportUri = boardTask?.report_uri ?? (version === null ? task?.report_uri : null);
  const explanation = taskEndExplanation(viewState);
  const cacheHit = boardTask?.usage.cache_hit_tokens ?? 0;
  const cacheInput = cacheHit + (boardTask?.usage.cache_miss_tokens ?? 0);
  const cachePercent = cacheInput ? `${(cacheHit / cacheInput * 100).toFixed(1)}%` : '—';
  const steps = Object.values(viewState.agents).reduce((count, agent) => count + agent.steps, 0);
  const workers = agents.filter((agent) => agent.taskType !== 'close' && ['running', 'concluding'].includes(agent.status)).length;
  const workerLimit = Number(boardTask?.budget.max_concurrent_agents) || '—';
  const cutoffAt = version === null ? null : viewEvents.at(-1)?.created_at;
  const duration = taskDuration(boardTask?.startedAt, boardTask?.finishedAt, cutoffAt, now);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.topline}><Link to="/tasks" className={styles.back}>← 返回任务</Link><span className={styles.taskId}>{taskId.slice(0, 8)}</span></div>
        <div className={styles.titleRow}>
          <div className={styles.titleGroup}><h1>{boardTask?.goal ?? task?.goal ?? '探索任务'}</h1><div className={styles.titleMeta}><span className={`${controls.badge} ${statusClass(status)}`}>{taskStatusLabel(status)}</span><span>运行 {duration}</span><span>花费 {formatCost(usedCost)}{maxCost === undefined ? '' : ` / ${formatCost(maxCost)}`}</span><span className={styles.connection} data-state={board.connection}><span aria-hidden="true" />{connectionText[board.connection]}</span></div></div>
          <div className={styles.actions}><button type="button" className={controls.button} onClick={() => setRecordsOpen(true)}>复盘记录</button>
            {reportUri && <Link to={`/tasks/${encodeURIComponent(taskId)}/report`} className={controls.button}>查看报告</Link>}
            {(boardTask?.workspace_uri ?? (version === null ? task?.workspace_uri : null)) && <a href={`/api/tasks/${encodeURIComponent(taskId)}/workspace`} className={controls.button}>下载归档</a>}
            {status === 'created' && <button type="button" className={`${controls.button} ${controls.primary}`} onClick={() => void changeStatus('start')} disabled={action !== null || version !== null}>{action === 'start' ? '正在启动…' : '启动任务'}</button>}
            {['created', 'provisioning', 'running'].includes(status) && <button type="button" className={`${controls.button} ${controls.danger}`} onClick={() => void changeStatus('stop')} disabled={action !== null || version !== null}>{action === 'stop' ? '正在停止…' : '停止'}</button>}
          </div>
        </div>
        <hr className={styles.statusDivider} />
        <div className={styles.runSummary}>
          {explanation && <p className={styles.endReason} title={explanation}>结束原因：{explanation}</p>}
          <span className={styles.runStats} aria-label="运行统计">对象 {totalObjects} · 并发 {workers}/{workerLimit} · 模型调用 {steps} · 缓存命中 {cachePercent} · 预算 {Math.round(costProgress)}%</span>
        </div>
        {version !== null && <div className={styles.history} role="status"><strong>历史回放 · v{version}</strong><span>已接收 {board.events.length - viewEvents.length} 条后续事件；当前视图只读。</span><button type="button" onClick={() => setVersion(null)}>返回实时</button></div>}
        {(actionError || (board.error && !unauthorized)) && <div className={styles.notice} role="alert"><span>{actionError || board.error?.message}</span>{board.error && <button type="button" onClick={() => window.location.reload()}>重新连接</button>}</div>}
        {board.connection === 'reconnecting' && !board.error && <div className={styles.reconnect} role="status">实时连接暂时中断，正在自动重连；已提交的内容会在恢复后补齐。</div>}
      </header>

      <div className={styles.agentBar} role="group" aria-label="全部 Agent">
        <strong>Agent <span>{agents.length}</span></strong>
        <div className={styles.agentList}>{agents.length ? agents.map((agent) => <button key={agent.id} ref={(node) => { if (node) agentButtons.current.set(agent.id, node); else agentButtons.current.delete(agent.id); }} type="button" className={styles.agentItem} style={agentStyle(numbers[agent.id])} aria-pressed={selectedId === agent.id} onClick={() => setSelectedId(agent.id)}><span>{agentLabel(agent.id, numbers)}</span><small>{agentRole(agent)} · {agentStatusLabel[agent.status]}</small><small className={styles.contribution}>{contributions[agent.id]?.facts.length ?? 0} Fact · {contributions[agent.id]?.intents.length ?? 0} Intent{contributions[agent.id]?.judgments ? ` · ${contributions[agent.id].judgments} 次裁定` : ''}</small></button>) : <span className={styles.agentEmpty}>尚无 Agent，任务启动后会出现在这里。</span>}</div>
      </div>

      <div className={`${styles.workspace} ${selectedId ? styles.withPanel : ''}`}>
        <section className={styles.canvasArea} aria-label="黑板关系图">
          {board.loading ? <div className={styles.canvasLoading} role="status" aria-label="正在同步关系图"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div> : <TopologyFlowCanvas key={taskId} state={viewState} agentNumbers={numbers} selectedId={selectedId} onSelect={setSelectedId} />}
        </section>
        {selectedAgent ? <AgentConversation contribution={contributions[selectedAgent.id]} color={agentStyle(numbers[selectedAgent.id])} onSelect={setSelectedId} key={`${taskId}-${selectedAgent.id}`} taskId={taskId} agent={selectedAgent} label={agentLabel(selectedAgent.id, numbers)} events={viewEvents} state={viewState} historical={version !== null} onClose={() => closeAgent(selectedAgent.id)} /> : selectedId ? <DetailPanel taskId={taskId} state={viewState} selectedId={selectedId} onSelect={setSelectedId} historical={version !== null} agentNumbers={numbers} /> : null}
      </div>
      <WorkbenchDrawer open={recordsOpen} onClose={() => setRecordsOpen(false)} taskId={taskId} state={viewState} events={viewEvents} allEvents={board.events} version={version} onVersion={setVersion} onSelect={setSelectedId} agentNumbers={numbers} />
    </div>
  );
}

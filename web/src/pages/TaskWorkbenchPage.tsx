import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { useBoard } from '../board/store';
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
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [action, setAction] = useState<'start' | 'stop' | null>(null);
  const [actionError, setActionError] = useState('');
  const taskQuery = useQuery({ queryKey: ['task', taskId], queryFn: () => api.getTask(taskId!), enabled: Boolean(taskId) });
  const board = useBoard(taskId);
  const unauthorized = (taskQuery.error instanceof ApiError && taskQuery.error.status === 401) || (board.error instanceof ApiError && board.error.status === 401);

  useEffect(() => { setSelectedId(null); }, [taskId]);
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
  if (taskQuery.isLoading && board.loading) return <div className={styles.loading} role="status" aria-label="正在加载任务"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div>;
  if (taskQuery.isError && !unauthorized) return <div className={styles.errorState} role="alert"><h1>无法打开任务</h1><p>{taskQuery.error instanceof ApiError ? taskQuery.error.message : '请稍后重试。'}</p><div><button type="button" className={controls.button} onClick={() => void taskQuery.refetch()}>重试</button><Link to="/tasks" className={controls.button}>返回任务列表</Link></div></div>;

  const task = taskQuery.data;
  const boardTask = board.state.task;
  const status = boardTask?.status ?? task?.status ?? 'created';
  const acceptance = Object.values(board.state.acceptance);
  const met = acceptance.filter((item) => item.status === 'met').length;
  const usedCost = boardTask?.usage.cost ?? task?.usage?.cost ?? 0;
  const maxCost = boardTask?.budget.max_cost;
  const costLimit = Number(maxCost);
  const costProgress = Number.isFinite(costLimit) && costLimit > 0 ? Math.min(100, Math.max(0, Number(usedCost) / costLimit * 100)) : 0;
  const reportUri = boardTask?.report_uri ?? task?.report_uri;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.topline}><Link to="/tasks" className={styles.back}>← 返回任务</Link><span className={styles.taskId}>{taskId.slice(0, 8)}</span></div>
        <div className={styles.titleRow}>
          <div className={styles.titleGroup}><div className={styles.titleMeta}><span className={`${controls.badge} ${statusClass(status)}`}>{taskStatusLabel(status)}</span><span className={styles.connection} data-state={board.connection}><span aria-hidden="true" />{connectionText[board.connection]}</span></div><h1>{boardTask?.goal ?? task?.goal ?? '探索任务'}</h1></div>
          <div className={styles.actions}>
            {reportUri && <a href={`/api/tasks/${encodeURIComponent(taskId)}/report`} target="_blank" rel="noopener noreferrer" className={controls.button}>查看报告</a>}
            {task?.workspace_uri && <a href={`/api/tasks/${encodeURIComponent(taskId)}/workspace`} className={controls.button}>下载归档</a>}
            {status === 'created' && <button type="button" className={`${controls.button} ${controls.primary}`} onClick={() => void changeStatus('start')} disabled={action !== null}>{action === 'start' ? '正在启动…' : '启动任务'}</button>}
            {['created', 'provisioning', 'running'].includes(status) && <button type="button" className={`${controls.button} ${controls.danger}`} onClick={() => void changeStatus('stop')} disabled={action !== null}>{action === 'stop' ? '正在停止…' : '停止'}</button>}
          </div>
        </div>
        <div className={styles.summary}>
          <div className={styles.acceptanceBlock}><div className={styles.summaryHeading}><strong>验收进度</strong><span>{met} / {acceptance.length} 已满足</span></div>{acceptance.length ? <ul className={styles.acceptance}>{acceptance.map((item) => <li key={item.id}><span className={`${controls.badge} ${item.status === 'met' ? controls.badgeSuccess : controls.badgeWarning}`}>{item.id} · {item.status === 'met' ? '已满足' : '未满足'}</span><span title={item.desc}>{item.desc}</span>{item.reason && <small title={item.reason}>裁定：{item.reason}</small>}{item.missing && <small title={item.missing}>缺口：{item.missing}</small>}</li>)}</ul> : <p className={styles.summaryEmpty}>验收信息正在同步。</p>}</div>
          <div className={styles.budgetBlock}><div className={styles.summaryHeading}><strong>金额预算</strong><span>{formatCost(usedCost)} / {maxCost === undefined ? '—' : formatCost(maxCost)}</span></div><progress max="100" value={costProgress} aria-label="金额预算已用比例" /><span className={styles.budgetNote}>已用 {Math.round(costProgress)}%</span></div>
        </div>
        {(actionError || (board.error && !unauthorized)) && <div className={styles.notice} role="alert"><span>{actionError || board.error?.message}</span>{board.error && <button type="button" onClick={() => window.location.reload()}>重新连接</button>}</div>}
        {board.connection === 'reconnecting' && !board.error && <div className={styles.reconnect} role="status">实时连接暂时中断，正在自动重连；已提交的内容会在恢复后补齐。</div>}
      </header>

      <div className={styles.workspace}>
        <section className={styles.canvasArea} aria-label="黑板关系图">
          {board.loading ? <div className={styles.canvasLoading} role="status" aria-label="正在同步关系图"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div> : <TopologyFlowCanvas key={taskId} state={board.state} selectedId={selectedId} onSelect={setSelectedId} />}
        </section>
        <DetailPanel taskId={taskId} state={board.state} selectedId={selectedId} onSelect={setSelectedId} />
      </div>
    </div>
  );
}

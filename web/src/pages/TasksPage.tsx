import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskView } from '../api/client';
import controls from '../styles/controls.module.css';
import { formatMoney, formatDate, taskStatusLabel } from './format';
import styles from './TasksPage.module.css';

function statusClass(status: string) {
  if (status === 'running') return controls.badgeInfo;
  if (status === 'finished') return controls.badgeSuccess;
  if (status === 'failed') return controls.badgeDanger;
  if (status === 'closing' || status === 'provisioning') return controls.badgeWarning;
  return '';
}

function metCount(task: TaskView): string {
  const states = Object.values(task.acceptance_state ?? {});
  return `${states.filter((item) => item && typeof item === 'object' && 'status' in item && item.status === 'met').length} / ${states.length}`;
}

export default function TasksPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [search, setSearch] = useState('');
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<TaskView | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);
  const query = useQuery({ queryKey: ['tasks'], queryFn: api.listTasks, refetchInterval: (query) => query.state.data?.some((task) => ['provisioning', 'running', 'closing'].includes(task.status) || Boolean('deleting' in task && task.deleting)) ? 2000 : false });
  const unauthorized = query.error instanceof ApiError && query.error.status === 401;

  useEffect(() => {
    if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname } });
  }, [unauthorized, navigate, location.pathname]);
  useEffect(() => {
    if (deleteTarget && !dialog.current?.open) dialog.current?.showModal();
    else if (!deleteTarget) dialog.current?.close();
  }, [deleteTarget]);

  async function removeTask() {
    if (!deleteTarget || deleting) return;
    setDeleting(true); setDeleteError('');
    try {
      await api.deleteTask(deleteTarget.id);
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: ['tasks'] });
    } catch (error) { setDeleteError(error instanceof Error ? error.message : '删除失败，请重试。'); }
    finally { setDeleting(false); }
  }

  const tasks = query.data ?? [];
  const visible = tasks.filter((task) => task.goal.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <div>
          <h1>任务</h1>
          <p>查看当前探索任务及其验收进度。</p>
        </div>
        {query.data && <span className={styles.headingCount}>{tasks.length} 项任务</span>}
      </div>

      {query.isLoading ? (
        <div className={styles.loading} aria-label="正在加载任务" role="status">
          <span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} />
        </div>
      ) : query.isError && !unauthorized ? (
        <div className={styles.state} role="alert">
          <h2>任务加载失败</h2>
          <p>{query.error instanceof ApiError ? query.error.message : '暂时无法连接黑板服务。'}</p>
          <button type="button" className={controls.button} onClick={() => void query.refetch()}>重试</button>
        </div>
      ) : tasks.length === 0 ? (
        <div className={styles.state}>
          <div className={styles.emptyGlyph} aria-hidden="true">◇</div>
          <h2>还没有探索任务</h2>
          <p>创建任务、设定验收条件后，事实和意图会在工作台形成关系图。</p>
          <Link to="/tasks/new" className={`${controls.button} ${controls.primary}`}>创建第一个任务</Link>
        </div>
      ) : (
        <>
          <div className={styles.toolbar}>
            <label htmlFor="task-search" className={styles.searchLabel}>查找任务</label>
            <input id="task-search" className={`${controls.input} ${styles.search}`} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="按目标筛选" type="search" />
            <span className={styles.count}>显示 {visible.length} 项任务</span>
          </div>
          {visible.length === 0 ? (
            <div className={styles.noMatches} role="status">没有匹配“{search}”的任务。<button type="button" onClick={() => setSearch('')}>清除筛选</button></div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead><tr><th scope="col">目标</th><th scope="col">状态</th><th scope="col">验收满足</th><th scope="col">已用金额</th><th scope="col">创建时间</th><th scope="col"><span className={styles.srOnly}>操作</span></th></tr></thead>
                <tbody>{visible.map((task) => (
                  <tr key={task.id}>
                    <td className={styles.goal}><Link to={`/tasks/${task.id}`} title={task.goal}>{task.goal}</Link><span className={styles.taskId}>{task.id.slice(0, 8)}</span></td>
                    <td data-label="状态"><span className={`${controls.badge} ${statusClass(task.status)}`}>{'deleting' in task && task.deleting ? '删除中' : taskStatusLabel(task.status)}</span></td>
                    <td data-label="验收满足">{metCount(task)}</td>
                    <td data-label="已用金额">{formatMoney(task.usage?.cost, task.cost_currency)}</td>
                    <td data-label="创建时间">{formatDate(task.created_at)}</td>
                    <td className={styles.open}><Link to={`/tasks/${task.id}`} aria-label={`查看任务：${task.goal}`}>查看<span aria-hidden="true"> →</span></Link><button type="button" className={styles.deleteButton} aria-label={`删除任务：${task.goal}`} disabled={!['created', 'finished', 'failed', 'stopped'].includes(task.status) || Boolean('deleting' in task && task.deleting)} title="任务结束后可删除，删除同时清除所有 Agent 会话" onClick={() => { setDeleteError(''); setDeleteTarget(task); }}>删除</button></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </>
      )}
      <dialog ref={dialog} className={styles.deleteDialog} aria-label="删除任务" onCancel={(event) => { if (deleting) event.preventDefault(); else setDeleteTarget(null); }} onClose={() => setDeleteTarget(null)}>
        <h2>删除这个任务？</h2><p>{deleteTarget?.goal}</p><p>黑板、证据、报告、归档及所有 Agent 会话将一起删除，无法恢复。</p>
        {deleteError && <p className={controls.error} role="alert">{deleteError}</p>}
        <div><button type="button" className={controls.button} disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button><button type="button" className={`${controls.button} ${controls.danger}`} disabled={deleting} onClick={() => void removeTask()}>{deleting ? '正在提交…' : '确认删除'}</button></div>
      </dialog>
    </div>
  );
}

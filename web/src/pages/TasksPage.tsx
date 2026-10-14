import { Alert, Button, Empty, Input, Modal, Pagination, Table, Tag, Tooltip, type TableColumnsType } from 'antd';
import { SearchOutlined, DeleteOutlined, ArrowRightOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { api, ApiError, type TaskView } from '../api/client';
import { activeDuration } from '../board/agents';
import { formatMoney, formatDate, taskStatusLabel, taskTitle } from './format';
import styles from './TasksPage.module.css';

function knownAmount(value: unknown, currency: string | null | undefined): string {
  const amount = typeof value === 'number' || typeof value === 'string' && value.trim() ? Number(value) : NaN;
  return Number.isFinite(amount) && amount >= 0 ? formatMoney(amount, currency) : '—';
}
function runtime(task: TaskView, now: number): string {
  if (!Number.isFinite(task.active_seconds) || task.active_seconds < 0 || task.active_since && !Number.isFinite(Date.parse(task.active_since))) return '—';
  return activeDuration(task.active_seconds, task.active_since, now);
}
function modeLabel(task: TaskView): string { return task.mode === 'ctf' ? 'CTF 团队' : '黑板探索'; }

export default function TasksPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const search = params.get('q') ?? '';
  const requestedPage = Number(params.get('page') ?? '1');
  const [compact, setCompact] = useState(() => matchMedia('(max-width: 650px)').matches);
  const [now, setNow] = useState(Date.now);
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<TaskView | null>(null);
  const [deleting, setDeleting] = useState(false);
  const deletePending = useRef(false);
  const [deleteError, setDeleteError] = useState('');
  const query = useQuery({ queryKey: ['tasks'], queryFn: api.listTasks, refetchInterval: query => query.state.data?.some(task => ['provisioning', 'running', 'closing'].includes(task.status) || task.deleting) ? 2000 : false });
  const unauthorized = query.error instanceof ApiError && query.error.status === 401;
  useEffect(() => { if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname + location.search } }); }, [unauthorized, navigate, location.pathname, location.search]);
  useEffect(() => {
    const media = matchMedia('(max-width: 650px)');
    const update = () => setCompact(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  const ticking = query.data?.some(task => Boolean(task.active_since)) ?? false;
  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ticking]);
  async function removeTask() {
    if (!deleteTarget || deletePending.current) return;
    deletePending.current = true; setDeleting(true); setDeleteError('');
    try { await api.deleteTask(deleteTarget.id); setDeleteTarget(null); await queryClient.invalidateQueries({ queryKey: ['tasks'] }); }
    catch (error) { setDeleteError(error instanceof Error ? error.message : '删除失败，请重试。'); }
    finally { deletePending.current = false; setDeleting(false); }
  }
  function changeSearch(value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set('q', value); else next.delete('q');
    next.delete('page'); setParams(next, { replace: true });
  }
  function changePage(page: number) {
    const next = new URLSearchParams(params);
    if (page > 1) next.set('page', String(page)); else next.delete('page');
    setParams(next, { replace: true });
  }
  const tasks = query.data ?? [];
  const visible = tasks.filter(task => `${task.name ?? ''} ${task.goal} ${task.id}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const pageSize = 20;
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const page = Number.isInteger(requestedPage) && requestedPage > 0 ? Math.min(requestedPage, pageCount) : 1;
  const returnTo = location.pathname + location.search;
  const taskName = (task: TaskView) => <div className={styles.identity}><Link to={`/tasks/${task.id}`} state={{ returnTo }} title={taskTitle(task)}>{taskTitle(task)}</Link><code title={task.id}>{task.id.slice(0, 8)}</code></div>;
  const status = (task: TaskView) => <Tag color={task.status === 'failed' ? 'error' : ['running', 'closing'].includes(task.status) ? 'processing' : 'default'}>{task.deleting ? '删除中' : task.status === 'finished' ? '已结束' : taskStatusLabel(task.status)}</Tag>;
  const costs = (task: TaskView) => <div className={styles.cost}><Tooltip title="已记录的任务模型费用；复盘费用单独统计"><span>{knownAmount(task.usage?.cost, task.cost_currency)}</span></Tooltip><small>预算 {knownAmount(task.budget?.max_cost, task.cost_currency)}</small></div>;
  const actions = (task: TaskView) => <div className={styles.rowActions}><Tooltip title="打开任务"><Link className={styles.open} to={`/tasks/${task.id}`} state={{ returnTo }} aria-label={`查看任务：${taskTitle(task)}`}><ArrowRightOutlined /></Link></Tooltip><Tooltip title="删除任务"><Button type="text" danger icon={<DeleteOutlined />} aria-label={`删除任务：${taskTitle(task)}`} disabled={!['created', 'finished', 'failed', 'stopped'].includes(task.status) || Boolean(task.deleting)} onClick={() => { setDeleteError(''); setDeleteTarget(task); }} /></Tooltip></div>;
  const columns: TableColumnsType<TaskView> = [
    { title: '任务名称', key: 'task', render: (_: unknown, task) => taskName(task) },
    { title: '模式', key: 'mode', width: 96, responsive: ['md'], render: (_: unknown, task) => <span className={styles.secondary}>{modeLabel(task)}</span> },
    { title: '运行状态', key: 'status', width: 100, render: (_: unknown, task) => status(task) },
    { title: '已用金额', key: 'cost', width: 140, render: (_: unknown, task) => costs(task) },
    { title: '运行耗时', key: 'runtime', width: 112, responsive: ['lg'], render: (_: unknown, task) => <span className={styles.numeric}>{runtime(task, now)}</span> },
    { title: '创建时间', key: 'date', width: 154, responsive: ['xl'], render: (_: unknown, task) => <span className={styles.secondary}>{formatDate(task.created_at)}</span> },
    { title: '', key: 'actions', width: 90, render: (_: unknown, task) => actions(task) },
  ];
  const empty = search ? <Empty description={`没有匹配“${search}”的任务`}><Button onClick={() => changeSearch('')}>清除筛选</Button></Empty> : <Empty description="还没有任务"><Button type="primary" onClick={() => navigate('/tasks/new')}>创建第一个任务</Button></Empty>;
  return <div className={styles.page}>
    <header className={styles.heading}><div><h1>任务</h1><span>{search ? `${visible.length} / ${tasks.length}` : tasks.length} 项任务</span></div><Input prefix={<SearchOutlined />} aria-label="查找任务" placeholder="搜索名称、目标或 ID" allowClear value={search} onChange={event => changeSearch(event.target.value)} className={styles.search} /></header>
    {query.isError && !unauthorized ? <Alert type="error" showIcon title="任务加载失败" description={query.error instanceof ApiError ? query.error.message : '暂时无法连接黑板服务。'} action={<Button onClick={() => void query.refetch()}>重试</Button>} /> : compact ? <>
      {query.isLoading ? <p role="status">正在读取任务…</p> : visible.length ? <ul className={styles.mobileList} aria-label="任务列表">{visible.slice((page - 1) * pageSize, page * pageSize).map(task => <li key={task.id}>
        {taskName(task)}<div className={styles.mobileMeta}>{status(task)}<span>{modeLabel(task)}</span><span>{runtime(task, now)}</span></div><div className={styles.mobileFooter}>{costs(task)}{actions(task)}</div><time className={styles.created} dateTime={task.created_at}>{formatDate(task.created_at)}</time>
      </li>)}</ul> : empty}
      {visible.length > pageSize && <Pagination aria-label="任务分页" simple current={page} pageSize={pageSize} total={visible.length} onChange={changePage} className={styles.pagination} />}
    </> : <Table rowKey="id" dataSource={visible} columns={columns} tableLayout="fixed" loading={query.isLoading} pagination={visible.length > pageSize ? { current: page, pageSize, onChange: changePage, showSizeChanger: false, showTotal: undefined } : false} locale={{ emptyText: empty }} />}
    <Modal title="删除任务" open={Boolean(deleteTarget)} onCancel={() => { if (!deleting) setDeleteTarget(null); }} onOk={() => void removeTask()} confirmLoading={deleting} okText="确认删除" cancelText="取消" okButtonProps={{ danger: true }} cancelButtonProps={{ disabled: deleting }} closable={!deleting} mask={{ closable: !deleting }} keyboard={!deleting}>
      <p>{deleteTarget ? taskTitle(deleteTarget) : ''}</p><p>黑板、证据、报告、归档及所有 Agent 会话将一起删除，无法恢复。</p>{deleteError && <Alert type="error" title={deleteError} showIcon />}
    </Modal>
  </div>;
}

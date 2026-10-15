import { Alert, Button, Dropdown, Empty, Grid, Input, Listy, Modal, Pagination, Segmented, Tag, Tooltip } from 'antd';
import { ApartmentOutlined, ArrowRightOutlined, ClockCircleOutlined, DeleteOutlined, FileTextOutlined, MoreOutlined, PlusOutlined, SearchOutlined, StopOutlined, TeamOutlined, WarningOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { api, ApiError, type TaskView } from '../api/client';
import { activeDuration } from '../board/agents';
import LoadingState from '../components/LoadingState';
import { formatMoney, formatDate, taskStatusLabel, taskTitle } from './format';
import styles from './TasksPage.module.css';

type TaskFilter = 'all' | 'active' | 'ended' | 'failed';
const filters: Array<{ value: TaskFilter; label: string }> = [
  { value: 'all', label: '全部' }, { value: 'active', label: '进行中' },
  { value: 'ended', label: '已结束' }, { value: 'failed', label: '异常' },
];
function matchesFilter(task: TaskView, filter: TaskFilter): boolean {
  if (filter === 'active') return ['created', 'provisioning', 'running', 'closing'].includes(task.status);
  if (filter === 'ended') return ['finished', 'stopped'].includes(task.status);
  return filter === 'all' || task.status === 'failed';
}
function knownAmount(value: unknown, currency: string | null | undefined): string {
  const amount = typeof value === 'number' || typeof value === 'string' && value.trim() ? Number(value) : NaN;
  return Number.isFinite(amount) && amount >= 0 ? formatMoney(amount, currency) : '—';
}
function runtime(task: TaskView, now: number): string {
  if (!Number.isFinite(task.active_seconds) || task.active_seconds < 0 || task.active_since && !Number.isFinite(Date.parse(task.active_since))) return '—';
  return activeDuration(task.active_seconds, task.active_since, now);
}

export default function TasksPage() {
  const navigate = useNavigate();
  const screens = Grid.useBreakpoint();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const search = params.get('q') ?? '';
  const filter = filters.find(item => item.value === params.get('view'))?.value ?? 'all';
  const requestedPage = Number(params.get('page') ?? '1');
  const [now, setNow] = useState(Date.now);
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<TaskView | null>(null);
  const actionButtons = useRef(new Map<string, HTMLElement>());
  const listHeading = useRef<HTMLHeadingElement>(null);
  const deleteReturn = useRef<{ id: string; completed: boolean } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const deletePending = useRef(false);
  const [deleteError, setDeleteError] = useState('');
  const query = useQuery({ queryKey: ['tasks'], queryFn: api.listTasks, refetchInterval: query => query.state.data?.some(task => ['provisioning', 'running', 'closing'].includes(task.status) || task.deleting) ? 2000 : false });
  const unauthorized = query.error instanceof ApiError && query.error.status === 401;
  const hasData = query.data !== undefined;
  useEffect(() => { if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname + location.search } }); }, [unauthorized, navigate, location.pathname, location.search]);
  const ticking = query.data?.some(task => Boolean(task.active_since)) ?? false;
  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ticking]);
  async function removeTask() {
    if (!deleteTarget || deletePending.current) return;
    deletePending.current = true; setDeleting(true); setDeleteError('');
    try { await api.deleteTask(deleteTarget.id); if (deleteReturn.current) deleteReturn.current.completed = true; setDeleteTarget(null); await queryClient.invalidateQueries({ queryKey: ['tasks'] }); }
    catch (error) { setDeleteError(error instanceof Error ? error.message : '删除失败，请重试。'); }
    finally { deletePending.current = false; setDeleting(false); }
  }
  function changeSearch(value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set('q', value); else next.delete('q');
    next.delete('page'); setParams(next, { replace: true });
  }
  function changeFilter(value: TaskFilter) {
    const next = new URLSearchParams(params);
    if (value === 'all') next.delete('view'); else next.set('view', value);
    next.delete('page'); setParams(next, { replace: true });
  }
  function changePage(page: number) {
    const next = new URLSearchParams(params);
    if (page > 1) next.set('page', String(page)); else next.delete('page');
    setParams(next, { replace: true });
  }
  const matching = useMemo(() => (query.data ?? []).filter(task => `${task.name ?? ''} ${task.goal} ${task.id}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())), [query.data, search]);
  const visible = useMemo(() => matching.filter(task => matchesFilter(task, filter)), [matching, filter]);
  const pageSize = 20;
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const page = Number.isInteger(requestedPage) && requestedPage > 0 ? Math.min(requestedPage, pageCount) : 1;
  const returnTo = location.pathname + location.search;
  const openTask = (task: TaskView, report = false) => navigate(`/tasks/${encodeURIComponent(task.id)}${report ? '/report' : ''}`, { state: { returnTo } });
  function taskRow(task: TaskView) {
    const active = ['provisioning', 'running', 'closing'].includes(task.status);
    const ended = ['finished', 'stopped'].includes(task.status);
    const title = taskTitle(task);
    return <article className={styles.row} role="listitem" aria-label={title}>
      <Link className={styles.identity} to={`/tasks/${encodeURIComponent(task.id)}`} state={{ returnTo }} aria-label={title} aria-describedby={`task-mode-${task.id} task-id-${task.id}`} title={title}>
        <span className={styles.taskIcon} aria-hidden="true">{task.mode === 'ctf' ? <TeamOutlined aria-hidden="true" /> : <ApartmentOutlined aria-hidden="true" />}</span>
        <div className={styles.taskText}>
          <div className={styles.titleLine}><strong>{title}</strong><code id={`task-id-${task.id}`} title={task.id}>{task.id.slice(0, 8)}</code></div>
          <div className={styles.taskMeta}><span id={`task-mode-${task.id}`}>{task.mode === 'ctf' ? 'CTF 团队' : '黑板探索'}</span>{task.name?.trim() && task.goal.trim() !== task.name.trim() && <span className={styles.goal} title={task.goal}>{task.goal}</span>}</div>
        </div>
      </Link>
      <div className={styles.state}><Tag className={styles.statusTag} color={task.status === 'failed' ? 'error' : active ? 'processing' : 'default'} icon={task.status === 'failed' ? <WarningOutlined aria-hidden="true" /> : ended ? <StopOutlined aria-hidden="true" /> : <ClockCircleOutlined aria-hidden="true" />}>{task.deleting ? '删除中' : task.status === 'finished' ? '已结束' : taskStatusLabel(task.status)}</Tag></div>
      <div className={styles.usage}>
        <Tooltip title="已记录模型费用 / 任务预算，复盘费用另计"><span className={styles.cost}><span>{knownAmount(task.usage?.cost, task.cost_currency)}</span><small> / <span>{knownAmount(task.budget?.max_cost, task.cost_currency)}</span></small></span></Tooltip>
        <span className={styles.runtime}><ClockCircleOutlined aria-hidden="true" />{runtime(task, now)}</span>
      </div>
      <div className={styles.created}><span>创建于</span><time dateTime={task.created_at} title={`创建于 ${formatDate(task.created_at)}`}>{formatDate(task.created_at)}</time></div>
      <div className={styles.rowActions}>
        <Tooltip title="打开任务"><Link className={styles.open} to={`/tasks/${encodeURIComponent(task.id)}`} state={{ returnTo }} aria-label={`查看任务：${title}`}><ArrowRightOutlined aria-hidden="true" /></Link></Tooltip>
        <Dropdown trigger={['click']} menu={{ items: [
          { key: 'open', label: '打开任务', icon: <ArrowRightOutlined aria-hidden="true" /> },
          ...(task.report_uri ? [{ key: 'report', label: '查看报告', icon: <FileTextOutlined aria-hidden="true" /> }] : []),
          { type: 'divider' },
          { key: 'delete', label: '删除任务', icon: <DeleteOutlined aria-hidden="true" />, danger: true, disabled: deleting || !['created', 'finished', 'failed', 'stopped'].includes(task.status) || Boolean(task.deleting) },
        ], onClick: ({ key }) => { if (key === 'open') openTask(task); if (key === 'report') openTask(task, true); if (key === 'delete') { deleteReturn.current = { id: task.id, completed: false }; setDeleteError(''); setDeleteTarget(task); } } }}>
          <Button ref={element => { if (element) actionButtons.current.set(task.id, element); else actionButtons.current.delete(task.id); }} type="text" icon={<MoreOutlined aria-hidden="true" />} aria-label={`任务操作：${title}`} />
        </Dropdown>
      </div>
    </article>;
  }
  const filtered = search.trim() || filter !== 'all';
  const empty = filtered ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的任务"><Button onClick={() => { const next = new URLSearchParams(params); ['q', 'page', 'view'].forEach(key => next.delete(key)); setParams(next, { replace: true }); }}>清除筛选</Button></Empty> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有任务"><Button type="primary" onClick={() => navigate('/tasks/new')}>创建第一个任务</Button></Empty>;
  return <div className={styles.page}>
    <header className={styles.heading}><h1 ref={listHeading} tabIndex={-1}>任务</h1><Button type="primary" icon={<PlusOutlined aria-hidden="true" />} onClick={() => navigate('/tasks/new')}>创建任务</Button></header>
    <section className={styles.surface} aria-label="任务工作区">
      <div className={styles.toolbar}>
        <Segmented size="small" aria-label="任务状态筛选" value={filter} options={filters.map(item => ({ value: item.value, label: <span className={styles.filterLabel}>{item.label}<span className={styles.filterCount}>{hasData ? matching.filter(task => matchesFilter(task, item.value)).length : '—'}</span></span> }))} onChange={value => changeFilter(value as TaskFilter)} />
        <Input prefix={<SearchOutlined aria-hidden="true" />} aria-label="查找任务" placeholder="搜索名称、目标或 ID" allowClear value={search} onChange={event => changeSearch(event.target.value)} className={styles.search} />
      </div>
      {query.isError && !unauthorized && <Alert className={styles.feedback} type="error" showIcon title={hasData ? "更新任务失败" : "任务读取失败"} description={query.error instanceof ApiError ? query.error.message : '暂时无法连接黑板服务。'} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
      {unauthorized || !hasData && !query.isError ? <LoadingState className={styles.placeholder} label="任务列表加载中" lines={7} /> : hasData && (visible.length ? <div role="list" aria-label="任务列表"><Listy items={visible.slice((page - 1) * pageSize, page * pageSize)} rowKey="id" virtual={false} itemRender={taskRow} classNames={{ item: styles.item }} /></div> : <div className={styles.empty}>{empty}</div>)}
      {hasData && visible.length > pageSize && <footer className={styles.footer}><span>第 {page} / {pageCount} 页</span><Pagination aria-label="任务分页" simple={screens.xs ? { readOnly: true } : false} current={page} pageSize={pageSize} total={visible.length} showSizeChanger={false} showLessItems onChange={changePage} /></footer>}
    </section>
    <Modal focusable={{ focusTriggerAfterClose: false }} afterClose={() => { const previous = deleteReturn.current; deleteReturn.current = null; if (previous) (previous.completed ? listHeading.current : actionButtons.current.get(previous.id) ?? listHeading.current)?.focus({ preventScroll: true }); }} title="删除任务" open={Boolean(deleteTarget)} onCancel={() => { if (!deleting) setDeleteTarget(null); }} onOk={() => void removeTask()} confirmLoading={deleting} okText="确认删除" cancelText="取消" okButtonProps={{ danger: true }} cancelButtonProps={{ disabled: deleting }} closable={!deleting} mask={{ closable: !deleting }} keyboard={!deleting}>
      <p>{deleteTarget ? taskTitle(deleteTarget) : ''}</p><p>黑板、证据、报告、归档及所有 Agent 会话将一起删除，无法恢复。</p>{deleteError && <Alert type="error" title={deleteError} showIcon />}
    </Modal>
  </div>;
}

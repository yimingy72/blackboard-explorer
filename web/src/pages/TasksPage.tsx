import { Alert, Button, Empty, Input, Modal, Table, Tag } from 'antd';
import { SearchOutlined, DeleteOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskView } from '../api/client';
import { formatMoney, formatDate, taskStatusLabel, taskTitle } from './format';
import styles from './TasksPage.module.css';
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
    const deletePending = useRef(false);
    const [deleteError, setDeleteError] = useState('');
    const query = useQuery({ queryKey: ['tasks'], queryFn: api.listTasks, refetchInterval: (query) => query.state.data?.some((task) => ['provisioning', 'running', 'closing'].includes(task.status) || Boolean('deleting' in task && task.deleting)) ? 2000 : false });
    const unauthorized = query.error instanceof ApiError && query.error.status === 401;
    useEffect(() => {
        if (unauthorized)
            navigate('/login', { replace: true, state: { from: location.pathname } });
    }, [unauthorized, navigate, location.pathname]);
    async function removeTask() {
        if (!deleteTarget || deletePending.current)
            return;
        deletePending.current = true;
        setDeleting(true);
        setDeleteError('');
        try {
            await api.deleteTask(deleteTarget.id);
            setDeleteTarget(null);
            await queryClient.invalidateQueries({ queryKey: ['tasks'] });
        }
        catch (error) {
            setDeleteError(error instanceof Error ? error.message : '删除失败，请重试。');
        }
        finally {
            deletePending.current = false;
            setDeleting(false);
        }
    }
    const tasks = query.data ?? [];
    const visible = tasks.filter((task) => `${task.name ?? ''} ${task.goal} ${task.id}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
    const columns = [
      { title: '任务', key: 'task', render: (_: unknown, task: TaskView) => <div className={styles.identity}><code title={task.id}>{task.id.slice(0, 8)}</code><div><Link to={`/tasks/${task.id}`} title={taskTitle(task)}>{taskTitle(task)}</Link><small>{task.mode === 'ctf' ? 'CTF 团队' : '黑板探索'}</small></div></div> },
      { title: '状态', key: 'status', width: 100, render: (_: unknown, task: TaskView) => <Tag color={task.status === 'running' ? 'processing' : task.status === 'failed' ? 'error' : task.status === 'finished' ? 'success' : 'default'}>{'deleting' in task && task.deleting ? '删除中' : taskStatusLabel(task.status)}</Tag> },
      { title: '验收', key: 'acceptance', width: 100, render: (_: unknown, task: TaskView) => task.mode === 'ctf' ? '按题协作' : metCount(task) },
      { title: '已用金额', key: 'cost', width: 120, render: (_: unknown, task: TaskView) => formatMoney(task.usage?.cost, task.cost_currency) },
      { title: '创建时间', key: 'date', width: 164, render: (_: unknown, task: TaskView) => formatDate(task.created_at) },
      { title: '操作', key: 'actions', width: 112, render: (_: unknown, task: TaskView) => <div className={styles.rowActions}><Button type="link" href={`/tasks/${task.id}`} aria-label={`查看任务：${taskTitle(task)}`}>查看</Button><Button type="text" danger icon={<DeleteOutlined />} aria-label={`删除任务：${taskTitle(task)}`} disabled={!['created', 'finished', 'failed', 'stopped'].includes(task.status) || Boolean('deleting' in task && task.deleting)} onClick={() => { setDeleteError(''); setDeleteTarget(task); }} /></div> },
    ];
    return <div className={styles.page}>
      <header className={styles.heading}><div><h1>任务</h1><span>{tasks.length} 项任务</span></div><Input prefix={<SearchOutlined />} aria-label="查找任务" placeholder="搜索名称、目标或 ID" allowClear value={search} onChange={(event) => setSearch(event.target.value)} className={styles.search} /></header>
      {query.isError && !unauthorized ? <Alert type="error" showIcon title="任务加载失败" description={query.error instanceof ApiError ? query.error.message : '暂时无法连接黑板服务。'} action={<Button onClick={() => void query.refetch()}>重试</Button>} /> :
      <Table rowKey="id" dataSource={visible} columns={columns} loading={query.isLoading} pagination={tasks.length > 20 ? { pageSize: 20, showSizeChanger: false } : false} scroll={{ x: 900 }} locale={{ emptyText: search ? <Empty description={`没有匹配“${search}”的任务`}><Button onClick={() => setSearch('')}>清除筛选</Button></Empty> : <Empty description="还没有任务"><Button type="primary" href="/tasks/new">创建第一个任务</Button></Empty> }} />}
      <Modal title="删除任务" open={Boolean(deleteTarget)} onCancel={() => { if (!deleting) setDeleteTarget(null); }} onOk={() => void removeTask()} confirmLoading={deleting} okText="确认删除" cancelText="取消" okButtonProps={{ danger: true }} cancelButtonProps={{ disabled: deleting }} closable={!deleting} mask={{closable:!deleting}} keyboard={!deleting}>
        <p>{deleteTarget ? taskTitle(deleteTarget) : ''}</p><p>黑板、证据、报告、归档及所有 Agent 会话将一起删除，无法恢复。</p>{deleteError && <Alert type="error" title={deleteError} showIcon />}
      </Modal>
    </div>;
}

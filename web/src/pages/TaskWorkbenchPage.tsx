import { lazy, Suspense, useEffect } from 'react';
import { Button } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { taskQueryOptions } from '../api/queries';
import LoadingState from '../components/LoadingState';

const CtfWorkbench = lazy(() => import('../ctf/CtfWorkbench'));
const BlackboardWorkbench = lazy(() => import('./BlackboardWorkbenchPage'));

export default function TaskWorkbenchPage() {
    const { taskId } = useParams<{
        taskId: string;
    }>();
    const navigate = useNavigate();
    const task = useQuery({ ...taskQueryOptions(taskId ?? ''), enabled: Boolean(taskId) });
    useEffect(() => { if (task.error instanceof ApiError && task.error.status === 401)
        navigate('/login', { replace: true }); }, [task.error, navigate]);
    if (!taskId)
        return <p role="alert">未指定任务。</p>;
    if (task.isLoading)
        return <LoadingState page label="任务信息加载中" lines={6} />;
    if (!task.data)
        return <div role="alert"><p>{task.error?.message ?? '无法读取任务。'}</p><Button onClick={() => void task.refetch()} htmlType={"button"}>重试</Button> <Link to="/tasks">返回任务列表</Link></div>;
    return task.data.mode === 'ctf'
        ? <Suspense fallback={<LoadingState page label="团队协作页加载中" lines={6} />}><CtfWorkbench key={taskId} taskId={taskId} initialTask={task.data}/></Suspense>
        : <Suspense fallback={<LoadingState page label="黑板关系图加载中" lines={5} />}><BlackboardWorkbench key={taskId}/></Suspense>;
}

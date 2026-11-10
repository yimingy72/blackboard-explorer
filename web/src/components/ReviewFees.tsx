import { Button, Collapse, Table } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';

export default function ReviewFees({ taskId, agentId, direct }: {
    taskId?: string; agentId?: string; direct?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const directory = useQuery({ queryKey: ['pi-models'], queryFn: api.piModels, enabled: open && direct === undefined });
    const piMode = direct ?? directory.data?.enabled;
    const directFees = useQuery({
        queryKey: ['pi-review-fees'],
        queryFn: api.piReviewFees,
        enabled: open && piMode === true,
        refetchInterval: query => open && query.state.data?.unknown_calls ? 4000 : false,
    });
    const fees = useQuery({
        queryKey: ['gateway-review-fees', taskId],
        queryFn: () => api.gatewayReviewFees(taskId),
        enabled: open && piMode === false,
        refetchInterval: query => open && query.state.data?.pending ? 4000 : false,
    });
    const rows = fees.data?.calls.filter(item => !agentId || item.agent_id === agentId);
    return <Collapse size="small" ghost activeKey={open ? ['fees'] : []} onChange={value => setOpen(value.includes('fees'))}
        items={[{ key: 'fees', label: '复盘费用', children: <>
            {(piMode === undefined && directory.isPending || piMode === true && directFees.isPending || piMode === false && fees.isPending) && <p role="status">正在读取费用…</p>}
            {directory.isError && <p role="alert">费用目录暂不可用。<Button type="link" onClick={() => void directory.refetch()}>重试</Button></p>}
            {piMode === true && directFees.isError && <p role="alert">复盘费用暂不可用。<Button type="link" onClick={() => void directFees.refetch()}>重试</Button></p>}
            {piMode === true && directFees.data && <>
                <p>本账户复盘费用：{directFees.data.unknown_calls && Number(directFees.data.estimated_usd) === 0
                    ? '待核' : `估算 ${directFees.data.estimated_usd} USD`}
                    {directFees.data.unknown_calls > 0 && ` · ${directFees.data.unknown_calls} 笔待核`}
                    {directFees.data.partial && ' · 站点覆盖不完整'}
                </p>
                <p>只汇总当前账户可读取的复盘费用，不计入任务预算。</p>
            </>}
            {piMode === false && fees.isError && <p role="alert">费用暂不可用。<Button type="link" onClick={() => void fees.refetch()}>重试</Button></p>}
            {piMode === false && fees.data && <>
                <p>{taskId ? '本任务' : '两站共用复盘密钥'}已确认费用：{fees.data.confirmed_usd == null ? '暂无已确认记录' : `${fees.data.confirmed_usd} USD`}
                    {fees.data.pending > 0 && ` · ${fees.data.pending} 笔待核`}
                    {fees.data.more && ' · 明细仅显示最近记录'}
                </p>
                <p>复盘费用单独记录，不计入求解或成果整理预算。</p>
                {!!rows?.length && <Table size="small" rowKey="attempt_id" dataSource={rows} pagination={{ pageSize: 8 }}
                    scroll={{ x: 540 }} columns={[
                        ...(taskId ? [] : [{ title: '任务', dataIndex: 'task_id' }]),
                        { title: 'Agent', dataIndex: 'agent_id' },
                        { title: '模型', dataIndex: 'model_alias' },
                        { title: '会话', dataIndex: 'conversation_id', ellipsis: true },
                        { title: '费用（USD）', dataIndex: 'cost_usd', render: value => value ?? '待核' },
                    ]} />}
            </>}
        </> }]} />;
}

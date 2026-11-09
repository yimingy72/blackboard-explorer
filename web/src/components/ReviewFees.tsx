import { Button, Collapse, Table } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';

export default function ReviewFees({ taskId, agentId }: { taskId?: string; agentId?: string }) {
    const [open, setOpen] = useState(false);
    const fees = useQuery({
        queryKey: ['gateway-review-fees', taskId],
        queryFn: () => api.gatewayReviewFees(taskId),
        enabled: open,
        refetchInterval: query => open && query.state.data?.pending ? 4000 : false,
    });
    const rows = fees.data?.calls.filter(item => !agentId || item.agent_id === agentId);
    return <Collapse size="small" ghost activeKey={open ? ['fees'] : []} onChange={value => setOpen(value.includes('fees'))}
        items={[{ key: 'fees', label: '复盘费用', children: <>
            {fees.isPending && <p role="status">正在读取 AgentGateway 费用…</p>}
            {fees.isError && <p role="alert">费用暂不可用。<Button type="link" onClick={() => void fees.refetch()}>重试</Button></p>}
            {fees.data && <>
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

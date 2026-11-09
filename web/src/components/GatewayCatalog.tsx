import { Alert, Button, Empty, Table, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import LoadingState from './LoadingState';
import ReviewFees from './ReviewFees';
import styles from '../pages/ProfilesPage.module.css';
import controls from '../styles/controls.module.css';

export default function GatewayCatalog() {
    const directory = useQuery({ queryKey: ['gateway-models'], queryFn: api.gatewayModels });
    if (directory.isLoading) return <LoadingState label="正在读取 AgentGateway 模型目录" />;
    const data = directory.data;
    return <section className={styles.catalogEditor} aria-label="AgentGateway 模型">
        <div className={styles.contentHeader}>
            <h2 className={styles.sectionTitle}>AgentGateway 模型</h2>
            <div className={styles.headerActions}>
                <Button onClick={() => void directory.refetch()} loading={directory.isFetching}>重新读取</Button>
                {data?.management_url && <Button type="primary" href={data.management_url}
                    target="_blank" rel="noopener noreferrer">打开 AgentGateway 管理</Button>}
            </div>
        </div>
        <div className={styles.formSection}>
            <p className={controls.hint}>模型连接、凭据和费率在 AgentGateway 管理。这里显示已应用的版本，供新任务选择。</p>
            {(directory.isError || data?.status === 'unavailable') &&
                <Alert type="warning" title="暂时无法确认 AgentGateway 模型目录" description="目录恢复前不能选择新模型。已有任务记录仍可查看。" showIcon />}
            {data?.maintenance && <Alert type="info" title="AgentGateway 正在维护，暂不接受新任务或新模型调用" showIcon />}
            {!data?.management_url && <p className={controls.hint}>如需修改模型，请从部署提供的 AgentGateway 管理入口登录。</p>}
            {Boolean(data?.models.length) && <Table size="small" rowKey="alias" dataSource={data?.models}
                pagination={false} scroll={{ x: 560 }} columns={[
                    { title: '模型', dataIndex: 'label' },
                    { title: '版本别名', dataIndex: 'alias', render: value => <code>{value}</code> },
                    { title: '上下文', dataIndex: 'context_window', render: value => value.toLocaleString() },
                    { title: '能力', dataIndex: 'tool_call', render: (value, model) => <>
                        {value && <Tag>工具调用</Tag>}{model.input_modalities.includes('image') && <Tag>图片输入</Tag>}
                    </> },
                    { title: '状态', render: (_, model) => model.tool_call ? '可用于任务' : '未声明工具调用能力' },
                ]} />}
            {data?.status === 'ready' && !data.models.length && <Empty description="AgentGateway 尚无已应用模型" />}
        </div>
        <ReviewFees />
    </section>;
}

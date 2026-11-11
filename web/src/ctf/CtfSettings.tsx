import { Alert, Button, Collapse, Segmented, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import LoadingState from '../components/LoadingState';
import { usePublishedMcpServers } from '../components/mcpGrants';
import styles from '../pages/ProfilesPage.module.css';
import controls from '../styles/controls.module.css';

function RoleExternalMcp({ role }: { role: 'lead' | 'teammate' }) {
    const { query, servers } = usePublishedMcpServers(role);
    if (query.isLoading) return <LoadingState label="正在读取已发布的 MCP 服务" />;
    if (query.isError) return <Alert type="warning" title="无法读取此角色的 MCP 发布范围" action={<Button onClick={() => void query.refetch()}>重试</Button>} />;
    return <>
        <p className={controls.hint}>发布只决定可选范围。请在新建原生 CTF 任务时为角色明确选择工具；助手不继承。</p>
        {servers.length
            ? <ul className={styles.toolList}>{servers.map(server => <li key={server.name}>{server.label} · v{server.version}</li>)}</ul>
            : <p className={controls.hint}>此角色没有已发布的外部 MCP 服务。</p>}
        <Button size="small" href="/profiles?section=mcp">查看服务与工具详情</Button>
    </>;
}

export default function CtfSettings() {
    const query = useQuery({ queryKey: ['published-ctf-settings'], queryFn: api.getCtfWorkerSettings });
    const capabilities = useQuery({ queryKey: ['task-creation-capabilities'], queryFn: api.taskCreationCapabilities });
    const [role, setRole] = useState<'lead' | 'teammate'>('lead');
    if (query.isLoading) return <LoadingState label="正在读取原生 CTF 发布" />;
    if (query.isError) return <Alert type="error" title="无法读取原生 CTF 发布" action={<Button onClick={() => void query.refetch()}>重试</Button>} />;
    const published = query.data;
    if (!published?.published) return <Alert type="info" title="当前没有已发布的原生 CTF 环境" description="历史任务仍可查看。原生 CTF 任务使用部署管理员发布的角色与权限。" />;
    return <section className={styles.catalogEditor} aria-label="已发布 CTF 配置">
        <div className={styles.contentHeader}>
            <h2 className={styles.sectionTitle}>已发布角色</h2>
            <Tag>配置版本 {published.revision}</Tag>
        </div>
        <p className={controls.hint}>这里展示新建原生 CTF 任务实际使用的配置。已创建的任务保留原快照。</p>
        <Segmented aria-label="CTF 角色" value={role} onChange={setRole} options={[{ value: 'lead', label: 'Lead' }, { value: 'teammate', label: 'Worker' }]} />
        <Collapse ghost items={[
            { key: 'tools', label: '已发布的黑板工具', children: <ul className={styles.toolList}>{published.business_tools[role].map(tool => <li key={tool}><code>{tool}</code></li>)}</ul> },
            ...(capabilities.data?.native_ctf?.external_mcp ? [{
                key: 'external-mcp', label: '可选的外部 MCP 服务', children: <RoleExternalMcp role={role} />,
            }] : []),
            { key: 'prompt', label: '已发布的系统提示词', children: <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{published.prompt_templates[role]}</pre> },
        ]} />
        {published.task_network && <p className={controls.hint}>Lead 按任务说明选择默认网络或共享 VPN。</p>}
    </section>;
}

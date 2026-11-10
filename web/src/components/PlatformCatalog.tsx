import { Alert, App, Button, Checkbox, Collapse, Empty, Form, Input, Select, Space, Switch, Tag } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, type McpRole, type McpDiscovery, type McpServer, type McpServerInput, type McpTool } from '../api/client';
import LoadingState from './LoadingState';
import { MCP_ROLE_LABELS } from './mcpGrants';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

const blankServer: McpServerInput = { label: '', url: '', auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true };

function object(value: unknown): Record<string, unknown> {
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function schemaType(value: unknown) {
    if (typeof value === 'string') return value;
    if (Array.isArray(value) && value.every(item => typeof item === 'string')) return value.join(' / ');
    return '见原始 Schema';
}

function ToolDetails({ tool }: { tool: McpTool }) {
    const properties = Object.entries(object(tool.input_schema?.properties));
    const required = Array.isArray(tool.input_schema?.required) ? tool.input_schema.required : [];
    return <details className={styles.mcpToolDetail}>
        <summary>工具详情：<strong>{tool.name}</strong></summary>
        {tool.description && <p>{tool.description}</p>}
        {properties.length > 0 && <dl className={styles.mcpParameters}>
            {properties.map(([name, value]) => {
                const property = object(value);
                return <div key={name}>
                    <dt><code>{name}</code> · {schemaType(property.type)} · {required.includes(name) ? '必填' : '可选'}</dt>
                    {typeof property.description === 'string' && <dd>{property.description}</dd>}
                </div>;
            })}
        </dl>}
        <details><summary>完整输入 Schema</summary><pre>{JSON.stringify(tool.input_schema ?? {}, null, 2)}</pre></details>
        {tool.output_schema && <details><summary>完整输出 Schema</summary><pre>{JSON.stringify(tool.output_schema, null, 2)}</pre></details>}
        {tool.annotations && <details><summary>服务声明（不代表平台权限）</summary>
            <pre>{JSON.stringify(tool.annotations, null, 2)}</pre>
        </details>}
    </details>;
}

export default function PlatformCatalog({ onDirtyChange, onBusyChange }: {
    onDirtyChange: (dirty: boolean) => void;
    onBusyChange: (busy: boolean) => void;
}) {
    const { modal } = App.useApp();
    const client = useQueryClient();
    const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers });
    const capabilities = useQuery({ queryKey: ['mcp-capabilities'], queryFn: api.mcpCapabilities });
    const canManage = capabilities.data?.can_manage === true;
    const [name, setName] = useState('');
    const [server, setServer] = useState<McpServerInput | null>(null);
    const [original, setOriginal] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [discovery, setDiscovery] = useState<McpDiscovery | null>(null);
    const [chosen, setChosen] = useState<string[]>([]);
    const [chosenRoles, setChosenRoles] = useState<McpRole[]>([]);
    const [loadingTools, setLoadingTools] = useState(false);
    const inspection = useRef(0);
    const active = servers.data?.find(item => item.name === name);
    const dirty = server !== null && JSON.stringify(server) !== original;
    useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
    useEffect(() => { onBusyChange(saving || loadingTools); }, [saving, loadingTools, onBusyChange]);
    function choose(item?: McpServer, force = false) {
        const update = () => {
            const next: McpServerInput = item ? {
                label: item.label, url: item.url, auth_header: item.auth_header,
                auth_scheme: item.auth_scheme, enabled: item.enabled,
            } : blankServer;
            inspection.current += 1;
            setLoadingTools(false);
            setName(item?.name ?? '');
            setServer(next);
            setOriginal(JSON.stringify(next));
            setDiscovery(null);
            setChosen([]);
            setChosenRoles([]);
            setError('');
            setNotice('');
        };
        if (!force && dirty) {
            modal.confirm({ title: '放弃尚未保存的修改？', okText: '放弃修改', cancelText: '继续编辑', onOk: update });
        } else update();
    }
    function patch(change: Partial<McpServerInput>) {
        if (!server) return;
        setServer({ ...server, ...change });
        inspection.current += 1;
        setLoadingTools(false);
        setDiscovery(null);
        setChosen([]);
        setChosenRoles([]);
        setNotice('');
    }
    async function save() {
        if (!canManage || !server || saving || loadingTools) return;
        try {
            const url = new URL(server.url);
            if (!server.label.trim() || url.protocol !== 'https:') throw Error();
        } catch { setError('请填写名称和公网 HTTPS 地址。'); return; }
        setError('');
        setSaving(true);
        try {
            const payload = { ...server, secret: server.secret || undefined };
            const saved = name ? await api.saveMcpServer(name, payload) : await api.createMcpServer(payload);
            await client.invalidateQueries({ queryKey: ['mcp-servers'] });
            choose(saved, true);
            setNotice(`已保存草稿 v${saved.version}。测试并发布后，获准角色才能选择新版本。`);
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : '保存失败。');
        } finally { setSaving(false); }
    }
    async function inspect(item: McpServer, version: number, discover = false) {
        if (dirty || saving || loadingTools || (discover && !canManage)) return;
        const sequence = ++inspection.current;
        setLoadingTools(true);
        setDiscovery(null);
        setChosen([]);
        setChosenRoles([]);
        setError('');
        setNotice('');
        try {
            const result = discover ? await api.discoverMcp(item.name, version) : await api.listMcpTools(item.name, version);
            if (inspection.current === sequence) setDiscovery(result);
        } catch (cause) {
            if (inspection.current === sequence) setError(cause instanceof Error ? cause.message : '工具清单读取失败。');
        } finally {
            if (inspection.current === sequence) setLoadingTools(false);
        }
    }
    async function publish() {
        if (!active || !discovery?.discovery_digest || dirty || saving || !canManage || !chosen.length || !chosenRoles.length) return;
        setSaving(true);
        setError('');
        try {
            const published = await api.publishMcp(active.name, discovery.version, discovery.discovery_digest, chosen, chosenRoles);
            await client.invalidateQueries({ queryKey: ['mcp-servers'] });
            choose(published, true);
            setNotice(`已发布 v${published.version}。获准角色可选择已批准的工具。`);
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : '发布失败。');
        } finally { setSaving(false); }
    }
    if (servers.isLoading) return <LoadingState label="正在读取 MCP 服务" />;
    return <section className={styles.catalogEditor} aria-label="MCP 服务编辑">
        <Form component={false} disabled={saving}>
            <form className={styles.catalogForm} onSubmit={event => { event.preventDefault(); void save(); }}>
                <header className={styles.contentHeader}>
                    <div className={styles.connectionPicker}>
                        <Select aria-label="MCP 服务" className={styles.resourceSelect} value={name || undefined}
                            placeholder={server ? '新增服务' : '选择服务'} disabled={saving}
                            options={servers.data?.map(item => ({ value: item.name, label: item.label })) ?? []}
                            onChange={value => { const item = servers.data?.find(entry => entry.name === value); if (item) choose(item); }} />
                        {canManage && <Button type="text" icon={<PlusOutlined />} onClick={() => choose()} disabled={saving || loadingTools}>新增 MCP 服务</Button>}
                    </div>
                    {server && canManage && <div className={styles.headerActions}>
                        <Switch aria-label="启用此服务" size="small" checked={server.enabled} onChange={enabled => patch({ enabled })} />
                        <span className={controls.hint}>启用</span>
                        <Button type="primary" size="small" htmlType="submit" aria-busy={saving} loading={saving} disabled={saving || loadingTools || (!dirty && Boolean(name))}>保存草稿</Button>
                    </div>}
                </header>
                <p className={`${controls.hint} ${styles.feedback}`}>平台发起连接测试。普通任务由运行服务调用；原生 CTF 由黑板业务服务调用。外部请求不走任务沙箱或 VPN，也不计入任务流量采集。</p>
                {capabilities.isError && <Alert type="warning" title="无法读取管理权限；可查看已保存配置。" />}
                {capabilities.data && !canManage && <Alert type="info" title="当前为只读，发布需要平台管理权限。" />}
                {servers.isError && <Alert type="error" title="无法读取 MCP 服务" action={<Button onClick={() => void servers.refetch()}>重试</Button>} />}
                {error && <Alert type="error" title={error} showIcon />}
                {notice && <Alert role="status" type="success" title={notice} showIcon />}
                {!server && !servers.isError && <Empty className={styles.emptyDetail} description="添加或选择 MCP 服务" />}
                {server && <div className={styles.formSection}>
                    <p className={controls.hint}>外部服务及其认证密钥决定远端资源的访问范围。平台按任务与角色限制可调用的工具。</p>
                    <p className={controls.hint}>保存停用后，新任务不能再授权此服务；已有任务继续使用原来选定的版本，直到结束。保存仍启用的草稿会保留上次发布的版本。</p>
                    <fieldset className={`${styles.formGrid} ${styles.catalogFields}`} disabled={!canManage}>
                        <label className={controls.field}><span className={controls.label}>显示名称</span><Input value={server.label} onChange={e => patch({ label: e.target.value })} /></label>
                        <label className={`${controls.field} ${styles.spanTwo}`}><span className={controls.label}>Streamable HTTP URL</span><Input value={server.url} onChange={e => patch({ url: e.target.value })} /></label>
                        <label className={controls.field}><span className={controls.label}>认证 Header</span><Select aria-label="认证 Header" disabled={!canManage} value={server.auth_header} options={['Authorization', 'X-API-Key', 'Api-Key'].map(value => ({ value }))} onChange={auth_header => patch({ auth_header, auth_scheme: auth_header === 'Authorization' ? 'Bearer' : '' })} /></label>
                        <label className={controls.field}><span className={controls.label}>认证 Scheme</span><Input value={server.auth_scheme} onChange={e => patch({ auth_scheme: e.target.value })} /></label>
                        <label className={controls.field}><span className={controls.label}>认证密钥</span><Input.Password autoComplete="new-password" value={server.secret ?? ''} placeholder={active?.has_secret ? '已保存；留空沿用当前密钥' : '可留空'} onChange={e => patch({ secret: e.target.value })} /></label>
                        {active?.has_secret && canManage && <Checkbox checked={Boolean(server.clear_secret)} onChange={e => patch({ clear_secret: e.target.checked })}>清除当前密钥</Checkbox>}
                    </fieldset>
                    {active && <Space className={styles.mcpVersionStatus} wrap>
                        <Tag>{active.published ? '已发布' : '草稿'} v{active.version}</Tag>
                        {!!active.published_version && active.published_version !== active.version && <Tag>当前已发布 v{active.published_version}</Tag>}
                        <Tag>{active.has_secret ? '已保存认证密钥' : '未配置认证密钥'}</Tag>
                        {!active.enabled && <Tag>已停用</Tag>}
                    </Space>}
                    {active && <Collapse ghost size="small" items={[{ key: 'tools', label: '工具清单', children: <div className={styles.mcpTools}>
                        {dirty && <p className={controls.hint}>先保存修改，再读取工具清单或测试连接。</p>}
                        <Space wrap>
                            <Button loading={loadingTools} aria-busy={loadingTools} disabled={saving || loadingTools || dirty} onClick={() => void inspect(active, active.version)}>查看工具清单</Button>
                            {!!active.published_version && active.published_version !== active.version &&
                                <Button disabled={saving || loadingTools || dirty} onClick={() => void inspect(active, active.published_version!)}>查看已发布工具</Button>}
                            {canManage && !active.published && active.enabled && <Button loading={loadingTools}
                                disabled={saving || loadingTools || dirty} onClick={() => void inspect(active, active.version, true)}>测试连接并发现工具</Button>}
                        </Space>
                        {discovery && <>
                            <p className={controls.hint}>v{discovery.version} · {discovery.published ? '已发布' : discovery.status === 'ready' ? '连接成功，待选择并发布' : discovery.status === 'error' ? '连接测试失败' : '尚未测试连接'}</p>
                            {discovery.published && !!discovery.roles?.length && <p className={controls.hint}>授权角色：{discovery.roles.map(role => MCP_ROLE_LABELS[role]).join('、')}</p>}
                            {discovery.error && <Alert type="error" title={discovery.error} />}
                            {discovery.status === 'ready' && discovery.tools.length === 0 && <p>该服务没有公开工具。</p>}
                            {discovery.tools.length > 0 && <ul className={styles.mcpToolList}>{discovery.tools.map(tool => <li key={tool.name}>
                                {canManage && !discovery.published && discovery.status === 'ready' && <Checkbox
                                    disabled={saving}
                                    checked={chosen.includes(tool.name)}
                                    onChange={event => setChosen(current => event.target.checked ? [...current, tool.name] : current.filter(name => name !== tool.name))}
                                >批准 {tool.name}</Checkbox>}
                                <ToolDetails tool={tool} />
                            </li>)}</ul>}
                            {canManage && !discovery.published && discovery.status === 'ready' && <>
                                <fieldset className={styles.toolFieldset}><legend>授权角色</legend>
                                    <Space wrap>{capabilities.data?.external_roles?.map(role =>
                                        <Checkbox key={role} checked={chosenRoles.includes(role)} disabled={saving}
                                            onChange={event => setChosenRoles(current => event.target.checked
                                                ? [...current, role] : current.filter(item => item !== role))}>
                                            {MCP_ROLE_LABELS[role]}
                                        </Checkbox>
                                    )}</Space>
                                </fieldset>
                                <Space wrap><Button type="primary" loading={saving}
                                    disabled={saving || dirty || !chosen.length || !chosenRoles.length}
                                    onClick={() => void publish()}>发布已选工具</Button></Space>
                            </>}
                        </>}
                    </div> }]} />}
                </div>}
            </form>
        </Form>
    </section>;
}

import { Alert, App, Button, Checkbox, Collapse, Empty, Form, Input, Select, Switch } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, type McpServer, type McpServerInput, type McpTool } from '../api/client';
import LoadingState from './LoadingState';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

const blankServer: McpServerInput = { label: '', url: '', auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true };

export default function PlatformCatalog({ onDirtyChange, onBusyChange }: {
    onDirtyChange: (dirty: boolean) => void;
    onBusyChange: (busy: boolean) => void;
}) {
    const { modal } = App.useApp();
    const client = useQueryClient();
    const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers });
    const [name, setName] = useState('');
    const [server, setServer] = useState<McpServerInput | null>(null);
    const [original, setOriginal] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [tools, setTools] = useState<McpTool[] | null>(null);
    const [loadingTools, setLoadingTools] = useState(false);
    const inspection = useRef(0);
    const active = servers.data?.find(item => item.name === name);
    const dirty = server !== null && JSON.stringify(server) !== original;
    useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
    useEffect(() => { onBusyChange(saving); }, [saving, onBusyChange]);
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
            setTools(null);
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
        setTools(null);
        setNotice('');
    }
    async function save() {
        if (!server || saving) return;
        try {
            const url = new URL(server.url);
            if (!server.label.trim() || !['http:', 'https:'].includes(url.protocol)) throw Error();
        } catch { setError('请填写名称和有效的 Streamable HTTP URL。'); return; }
        setError('');
        setSaving(true);
        try {
            const payload = { ...server, secret: server.secret || undefined };
            const saved = name ? await api.saveMcpServer(name, payload) : await api.createMcpServer(payload);
            await client.invalidateQueries({ queryKey: ['mcp-servers'] });
            choose(saved, true);
            setNotice(`已保存 ${saved.label}。已有任务保持原配置。`);
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : '保存失败。');
        } finally { setSaving(false); }
    }
    async function inspect(item: McpServer) {
        if (dirty || saving) return;
        const sequence = ++inspection.current;
        setLoadingTools(true);
        setTools(null);
        setError('');
        try {
            const result = await api.listMcpTools(item.name, item.version);
            if (inspection.current === sequence) setTools(result.tools);
        } catch (cause) {
            if (inspection.current === sequence) setError(cause instanceof Error ? cause.message : '工具清单读取失败。');
        } finally {
            if (inspection.current === sequence) setLoadingTools(false);
        }
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
                        <Button type="text" icon={<PlusOutlined />} onClick={() => choose()} disabled={saving}>新增 MCP 服务</Button>
                    </div>
                    {server && <div className={styles.headerActions}>
                        <Switch aria-label="启用此服务" size="small" checked={server.enabled} onChange={enabled => patch({ enabled })} />
                        <span className={controls.hint}>启用</span>
                        <Button type="primary" size="small" htmlType="submit" loading={saving} disabled={!dirty && Boolean(name)}>保存</Button>
                    </div>}
                </header>
                <p className={controls.hint}>这里管理黑板 Worker 可使用的 MCP 服务；原生 CTF 只显示已发布的能力。</p>
                {servers.isError && <Alert type="error" title="无法读取 MCP 服务" action={<Button onClick={() => void servers.refetch()}>重试</Button>} />}
                {error && <Alert type="error" title={error} showIcon />}
                {notice && <Alert role="status" type="success" title={notice} showIcon />}
                {!server && !servers.isError && <Empty className={styles.emptyDetail} description="添加或选择 MCP 服务" />}
                {server && <div className={styles.formSection}>
                    <div className={styles.formGrid}>
                        <label className={controls.field}><span className={controls.label}>显示名称</span><Input value={server.label} onChange={e => patch({ label: e.target.value })} /></label>
                        <label className={`${controls.field} ${styles.spanTwo}`}><span className={controls.label}>Streamable HTTP URL</span><Input value={server.url} onChange={e => patch({ url: e.target.value })} /></label>
                        <label className={controls.field}><span className={controls.label}>认证 Header</span><Input value={server.auth_header} onChange={e => patch({ auth_header: e.target.value })} /></label>
                        <label className={controls.field}><span className={controls.label}>认证 Scheme</span><Input value={server.auth_scheme} onChange={e => patch({ auth_scheme: e.target.value })} /></label>
                        <label className={controls.field}><span className={controls.label}>认证密钥</span><Input.Password autoComplete="new-password" value={server.secret ?? ''} placeholder={active?.has_secret ? '已保存；留空沿用当前密钥' : '可留空'} onChange={e => patch({ secret: e.target.value })} /></label>
                        {active?.has_secret && <Checkbox checked={Boolean(server.clear_secret)} onChange={e => patch({ clear_secret: e.target.checked })}>清除当前密钥</Checkbox>}
                    </div>
                    {active && <Collapse ghost size="small" items={[{ key: 'tools', label: '工具清单', children: <>
                        {dirty && <p className={controls.hint}>先保存修改，再读取工具清单。</p>}
                        <Button loading={loadingTools} disabled={saving || loadingTools || dirty} onClick={() => void inspect(active)}>查看工具清单</Button>
                        {tools && (tools.length ? <ul className={styles.toolList}>{tools.map(tool => <li key={tool.name}><strong>{tool.name}</strong><p>{tool.description}</p></li>)}</ul> : <p>该服务没有公开工具。</p>)}
                    </> }]} />}
                </div>}
            </form>
        </Form>
    </section>;
}

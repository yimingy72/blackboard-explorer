import { Alert, App, Button, Checkbox, Empty, Form, Input, Select, Switch, Tag } from 'antd';
import { ApiOutlined, ExperimentOutlined, PlusOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError, type McpRole, type McpDiscovery, type McpServer, type McpServerInput, type McpTool } from '../api/client';
import LoadingState from './LoadingState';
import { MCP_ROLE_LABELS } from './mcpGrants';
import { DetailHeading, SettingsFrame, SettingsHeading, SettingsRail } from '../settings/SettingsShell';
import { useSettingsLogin, useTrackSettings, type ChangeTracking } from '../settings/settingsChanges';
import { McpToolDrawer } from '../settings/ToolPermissions';
import styles from '../settings/Settings.module.css';

const blankServer: McpServerInput = { label: '', url: '', auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true };

const draftOf = (item?: McpServer): McpServerInput => item ? {
    label: item.label, url: item.url, auth_header: item.auth_header,
    auth_scheme: item.auth_scheme, enabled: item.enabled,
} : blankServer;

export default function PlatformCatalog(tracking: ChangeTracking) {
    const { modal } = App.useApp();
    const client = useQueryClient();
    const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers });
    const capabilities = useQuery({ queryKey: ['mcp-capabilities'], queryFn: api.mcpCapabilities });
    useSettingsLogin(servers.error ?? capabilities.error);
    const canManage = capabilities.data?.can_manage === true && !capabilities.isError && !servers.isError;
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
    const inspection = useRef(0), inFlight = useRef(false);
    const [detail, setDetail] = useState<{ tool: McpTool; source: string; scope: string } | null>(null);
    const active = servers.data?.find(item => item.name === name);
    const formDirty = server !== null && JSON.stringify(server) !== original;
    const dirty = formDirty || chosen.length > 0 || chosenRoles.length > 0;
    useTrackSettings(dirty, saving || loadingTools, tracking);
    useEffect(() => {
        const first = servers.data?.[0];
        if (!server && first) { setName(first.name); setServer(draftOf(first)); setOriginal(JSON.stringify(draftOf(first))); }
    }, [servers.data, server]);
    function choose(item?: McpServer, force = false) {
        if (inFlight.current && !force) return;
        const update = () => {
            const next = draftOf(item);
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
        if (!canManage || !server || inFlight.current || loadingTools) return;
        try {
            const url = new URL(server.url);
            if (!server.label.trim() || url.protocol !== 'https:') throw Error();
        } catch { setError('请填写名称和公网 HTTPS 地址。'); return; }
        setError('');
        inFlight.current = true;
        setSaving(true);
        try {
            const payload = { ...server, secret: server.secret || undefined };
            const saved = name ? await api.saveMcpServer(name, payload) : await api.createMcpServer(payload);
            await client.invalidateQueries({ queryKey: ['mcp-servers'] });
            choose(saved, true);
            setNotice(`已保存草稿 v${saved.version}。测试并发布后，获准角色才能选择新版本。`);
        } catch (cause) {
            setError(cause instanceof ApiError && cause.status === 409 ? '服务版本已更新。当前草稿已保留。' : cause instanceof Error ? cause.message : '保存失败。');
        } finally { inFlight.current = false; setSaving(false); }
    }
    async function inspect(item: McpServer, version: number, discover = false) {
        if (formDirty || saving || loadingTools || (discover && !canManage)) return;
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
        if (!active || !discovery?.discovery_digest || discovery.version !== active.version || formDirty || inFlight.current || !canManage || !chosen.length || !chosenRoles.length) return;
        inFlight.current = true;
        setSaving(true);
        setError('');
        try {
            const published = await api.publishMcp(active.name, discovery.version, discovery.discovery_digest, chosen, chosenRoles);
            await client.invalidateQueries({ queryKey: ['mcp-servers'] });
            choose(published, true);
            setNotice(`已发布 v${published.version}。获准角色可选择已批准的工具。`);
        } catch (cause) {
            setError(cause instanceof ApiError && cause.status === 409 ? '服务版本或发现结果已更新。已保留选择，请核对最新版本后再发布。' : cause instanceof Error ? cause.message : '发布失败。');
        } finally { inFlight.current = false; setSaving(false); }
    }
    async function reload() {
        if (inFlight.current || loadingTools) return;
        if (dirty && !await new Promise<boolean>(resolve => modal.confirm({
            title: '放弃草稿并重新读取？', okText: '放弃并读取', cancelText: '继续编辑', onOk: () => resolve(true), onCancel: () => resolve(false),
        }))) return;
        if (inFlight.current) return;
        inFlight.current = true; setSaving(true);
        try {
            const result = await servers.refetch({ throwOnError: true });
            const items = result.data ?? [];
            const next = items.find(item => item.name === name) ?? items[0];
            if (next) choose(next, true);
            else { choose(undefined, true); setServer(null); }
        } catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取 MCP 服务。'); }
        finally { inFlight.current = false; setSaving(false); }
    }
    const busy = saving || loadingTools;
    const canPublish = canManage && discovery?.status === 'ready' && !discovery.published && discovery.version === active?.version;
    const caption = (item: McpServer) => !item.enabled ? '已停用' : item.published ? '已发布 · v' + item.version : '草稿 · v' + item.version;
    return <div className={styles.page}>
      <SettingsHeading title="MCP 管理" description="登记外部服务，核对工具参数，指定工具可供哪些角色选用。" actions={<>
        <Button disabled={busy} onClick={() => void reload()}>重新读取</Button>
        {canManage && <Button icon={<PlusOutlined aria-hidden="true" />} onClick={() => choose()} disabled={busy}>新增 MCP 服务</Button>}
      </>} />
      {servers.isLoading ? <LoadingState label="正在读取 MCP 服务" /> : <>
        {capabilities.isError && <Alert className={styles.feedback} type="warning" title="无法读取管理权限；可查看已保存配置。"
          action={<Button onClick={() => void capabilities.refetch()}>重试</Button>} />}
        {capabilities.data && !canManage && !capabilities.isError && <Alert className={styles.feedback} type="info" title="当前为只读，发布需要平台管理权限。" />}
        {servers.isError && <Alert className={styles.feedback} type="error" title="无法读取 MCP 服务" />}
        <SettingsFrame>
          <SettingsRail title="服务列表" intro="注册、发现与发布工具" note="发现工具、发布范围与任务授权分别管理。"
            items={(servers.data ?? []).map(item => ({ id: item.name, title: item.label, caption: caption(item), icon: <ApiOutlined /> }))}
            active={name} disabled={busy} onSelect={value => {
              const item = servers.data?.find(entry => entry.name === value); if (item && value !== name) choose(item);
            }} />
          <section className={styles.main} aria-label="MCP 服务编辑">
            {!server ? <Empty className={styles.emptyPanel} description="尚未登记 MCP 服务" /> : <>
              <DetailHeading icon={<ApiOutlined />} title={active?.label ?? '新增 MCP 服务'} description="Streamable HTTP · 外部工具来源"
                actions={<>
                  {active && <Tag color={active.published && active.enabled ? 'processing' : undefined}>{caption(active)}</Tag>}
                  {active && canManage && !active.published && active.enabled &&
                    <Button icon={<ExperimentOutlined aria-hidden="true" />} loading={loadingTools} disabled={busy || dirty}
                      onClick={() => void inspect(active, active.version, true)}>测试连接并发现工具</Button>}
                  {canManage && <Button type="primary" form="mcp-config" htmlType="submit" loading={saving}
                    disabled={busy || (!formDirty && Boolean(name))}>保存草稿</Button>}
                </>} />
              <div className={styles.body}>
                {error && <Alert className={styles.feedback} type="error" title={error} showIcon />}
                {notice && <Alert className={styles.feedback} role="status" type="success" title={notice} showIcon />}
                <Form id="mcp-config" layout="vertical" disabled={!canManage || busy} onFinish={() => void save()}>
                  <section className={styles.formBlock}><h3>服务连接</h3><div className={styles.fields}>
                    <Form.Item label="显示名称" htmlFor="mcp-label" required><Input id="mcp-label" value={server.label} onChange={e => patch({ label: e.target.value })} /></Form.Item>
                    <Form.Item label="Streamable HTTP URL（公网 HTTPS）" htmlFor="mcp-url" required><Input id="mcp-url" value={server.url} onChange={e => patch({ url: e.target.value })} /></Form.Item>
                    <Form.Item label="认证 Header" htmlFor="mcp-header"><Select id="mcp-header" value={server.auth_header}
                      options={['Authorization', 'X-API-Key', 'Api-Key'].map(value => ({ value }))}
                      onChange={auth_header => patch({ auth_header, auth_scheme: auth_header === 'Authorization' ? 'Bearer' : '' })} /></Form.Item>
                    <Form.Item label="认证 Scheme" htmlFor="mcp-scheme"><Input id="mcp-scheme" value={server.auth_scheme} onChange={e => patch({ auth_scheme: e.target.value })} /></Form.Item>
                    <Form.Item label="认证密钥" htmlFor="mcp-key"><Input.Password id="mcp-key" autoComplete="new-password" value={server.secret ?? ''}
                      disabled={!canManage || busy || server.clear_secret} placeholder={active?.has_secret ? '留空沿用当前密钥' : '可留空'}
                      onChange={e => patch({ secret: e.target.value })} /></Form.Item>
                    <div className={styles.field}><span>启用此服务</span><Switch className={styles.fieldSwitch} aria-label="启用此服务" checked={server.enabled} onChange={enabled => patch({ enabled })} /></div>
                  </div>
                  {active?.has_secret && canManage && <Checkbox checked={Boolean(server.clear_secret)} onChange={event => {
                    if (!event.target.checked) patch({ clear_secret: undefined });
                    else modal.confirm({ title: '清除当前密钥？', content: '确认后仍需保存草稿，才会清除新版本的密钥。', okText: '确认清除', cancelText: '取消',
                        onOk: () => patch({ clear_secret: true, secret: undefined }) });
                  }}>清除当前密钥</Checkbox>}
                  <p className={styles.formHint}>平台发起连接测试；外部请求不经过任务沙箱或 VPN，也不计入任务流量采集。</p>
                  </section>
                </Form>
                <section className={styles.formBlock}><h3>工具清单</h3>
                  {!active ? <p className={styles.formHint}>保存草稿后，可以测试连接并发现工具。</p> : <>
                    <p className={styles.formHint}>{formDirty ? '先保存修改，再读取工具清单或测试连接。' : '发现工具只返回清单和 Schema；请选择要发布的工具。'}</p>
                    <div className={styles.actions}>
                      <Button loading={loadingTools} disabled={busy || dirty} onClick={() => void inspect(active, active.version)}>查看工具清单</Button>
                      {!!active.published_version && active.published_version !== active.version &&
                        <Button disabled={busy || dirty} onClick={() => void inspect(active, active.published_version!)}>查看已发布工具</Button>}
                    </div>
                    {discovery && <>
                      <p className={styles.formHint}>v{discovery.version} · {discovery.published ? '已发布'
                        : discovery.status === 'ready' ? '连接成功，待选择并发布' : discovery.status === 'error' ? '连接测试失败' : '尚未测试连接'}</p>
                      {discovery.error && <Alert type="error" title={discovery.error} />}
                      {discovery.status === 'ready' && discovery.tools.length === 0 && <p>该服务没有公开工具。</p>}
                      {discovery.tools.length > 0 && <ul className={styles.discoveredTools}>{discovery.tools.map(tool => <li key={tool.name}>
                        <div><strong>{tool.name}</strong>{tool.description && <p>{tool.description}</p>}</div>
                        <Button size="small" onClick={() => setDetail({ tool, source: active.label + ' · v' + discovery.version,
                            scope: discovery.published ? '已发布工具仍须由相应角色明确选用。' : '发现结果仅供审阅，尚未发布。' })}>查看 Schema</Button>
                        {canPublish && <Checkbox aria-label={'批准 ' + tool.name} disabled={busy} checked={chosen.includes(tool.name)}
                          onChange={event => setChosen(old => event.target.checked ? [...old, tool.name] : old.filter(name => name !== tool.name))}>纳入发布</Checkbox>}
                      </li>)}</ul>}
                    </>}
                  </>}
                </section>
                <section className={styles.formBlock}><h3>发布范围与角色授权</h3>
                  <p className={styles.releaseNote}>当前发布版本：{active?.published_version ? 'v' + active.published_version : '未发布'}
                    {!!active?.published_roles?.length && ' · ' + active.published_roles.map(role => MCP_ROLE_LABELS[role]).join('、')}</p>
                  <p className={styles.formHint}>发布决定可选范围。Explore 在角色配置逐工具选用，CTF 在新建任务时逐工具授权。</p>
                  <Link className={styles.textLink} to="/profiles?source=mcp">前往 Explore 工具权限</Link>
                  {canPublish && <div className={styles.publishControls}>
                    <fieldset className={styles.roleOptions}><legend>选择发布角色</legend>
                      <div className={styles.actions}>{capabilities.data?.external_roles.map(role =>
                        <Checkbox key={role} checked={chosenRoles.includes(role)} disabled={busy}
                          onChange={event => setChosenRoles(old => event.target.checked ? [...old, role] : old.filter(item => item !== role))}>{MCP_ROLE_LABELS[role]}</Checkbox>)}</div>
                    </fieldset>
                    <Button type="primary" loading={saving} disabled={busy || formDirty || !chosen.length || !chosenRoles.length}
                      onClick={() => void publish()}>发布已选工具</Button>
                  </div>}
                  {canPublish && (chosen.length > 0 || chosenRoles.length > 0) && <Button size="small" type="link" disabled={busy}
                    onClick={() => { setChosen([]); setChosenRoles([]); }}>清空发布选择</Button>}
                </section>
                <p className={styles.note}>停用只阻止新任务选用。已有任务继续使用原版本；保存仍启用的草稿会保留上次发布版本。</p>
              </div>
            </>}
          </section>
        </SettingsFrame>
      </>}
      <McpToolDrawer details={detail} onClose={() => setDetail(null)} />
    </div>;
}

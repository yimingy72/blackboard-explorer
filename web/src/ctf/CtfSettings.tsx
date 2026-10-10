import { Button, Checkbox, Collapse, Input, Select, Segmented, App, Form } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, ApiError, type WorkerTools } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';
type Role = 'lead' | 'teammate';
type Draft = {
    prompt: string;
    tools: WorkerTools;
};
type Settings = Awaited<ReturnType<typeof api.getCtfWorkerSettings>>;
type Binding = NonNullable<Settings['profile']['platform_tools']>[number];
const roles: Role[] = ['lead', 'teammate'];
const common = ['list_members', 'send_message', 'execute_command', 'list_challenges', 'get_challenge', 'create_challenge', 'update_challenge', 'list_records', 'append_record', 'register_artifact', 'request_help', 'record_candidate'];
const builtin = { lead: [...common, 'create_teammate', 'stop_teammate', 'resume_teammate', 'remove_teammate', 'finish_task', 'set_verification_required', 'confirm_messages'], teammate: common };
const from = (value: Settings, role: Role): Draft => ({ prompt: value.profile.prompt_templates[role], tools: value.profile.worker_tools[role] });
export default function CtfSettings({ onDirtyChange, onBusyChange }: {
    onDirtyChange: (dirty: boolean) => void;
    onBusyChange?: (busy: boolean) => void;
}) {
    const { modal } = App.useApp();
    const query = useQuery({ queryKey: ['ctf-worker-settings'], queryFn: api.getCtfWorkerSettings, refetchOnWindowFocus: false });
    const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers });
    const [snapshot, setSnapshot] = useState<Settings | null>(null);
    const [drafts, setDrafts] = useState<Record<Role, Draft> | null>(null);
    const [bindings, setBindings] = useState<Binding[]>([]);
    const [role, setRole] = useState<Role>('lead');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [inspection, setInspection] = useState<{
        key: string;
        tools: {
            name: string;
            description: string;
        }[];
    } | null>(null);
    const [loading, setLoading] = useState('');
    const sequence = useRef(0);
    useEffect(() => {
        if (query.data && !snapshot) {
            setSnapshot(query.data);
            setBindings(query.data.profile.platform_tools ?? []);
            setDrafts({ lead: from(query.data, 'lead'), teammate: from(query.data, 'teammate') });
        }
    }, [query.data, snapshot]);
    const changed = (item: Role) => Boolean(snapshot && drafts && JSON.stringify(drafts[item]) !== JSON.stringify(from(snapshot, item)));
    const bindingsDirty = Boolean(snapshot && JSON.stringify(bindings) !== JSON.stringify(snapshot.profile.platform_tools ?? []));
    const dirty = roles.some(changed) || bindingsDirty;
    useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
    useEffect(() => () => { sequence.current += 1; }, []);
    async function reload() {
        if (saving || dirty && !await new Promise<boolean>(resolve=>modal.confirm({title:'放弃未保存的 CTF 配置并重新读取？',okText:'放弃修改',cancelText:'继续编辑',onOk:()=>resolve(true),onCancel:()=>resolve(false)})))
            return;
        const result = await query.refetch();
        if (result.data) {
            setSnapshot(result.data);
            setBindings(result.data.profile.platform_tools ?? []);
            setDrafts({ lead: from(result.data, 'lead'), teammate: from(result.data, 'teammate') });
            setError('');
            setNotice('已重新读取。');
        }
    }
    async function save() {
        if (!snapshot || !drafts)
            return;
        for (const server of drafts[role].tools.mcp_servers ?? []) {
            if (server.allowed_tools == null) {
                setError('请读取 MCP 清单并逐项选择工具。');
                return;
            }
            for (const name of server.allowed_tools) {
                const binding = bindings.find((item) => item.server_name === server.name && item.server_version === server.version && item.tool_name === name);
                if (!binding || binding.purpose === 'unknown') {
                    setError(`请先为工具 ${name} 指定用途；未分类工具不会启用。`);
                    return;
                }
            }
        }
        setSaving(true); onBusyChange?.(true);
        setError('');
        setNotice('');
        try {
            const saved = await api.saveCtfWorkerSettings(role, snapshot.revision, drafts[role].prompt, drafts[role].tools, bindings);
            setSnapshot(saved);
            setBindings(saved.profile.platform_tools ?? []);
            setDrafts((current) => current ? { ...current, [role]: from(saved, role) } : null);
            setNotice('CTF 配置已保存，仅用于之后创建的任务。');
        }
        catch (cause) {
            setError(cause instanceof ApiError && cause.status === 409 ? '配置已被其他窗口更新；当前草稿已保留，请复制后重新读取。' : cause instanceof Error ? cause.message : '保存失败。');
        }
        finally {
            setSaving(false); onBusyChange?.(false);
        }
    }
    async function inspect(name: string, version: number) {
        const id = ++sequence.current;
        const key = `${name}@${version}`;
        setLoading(key);
        setInspection(null);
        setError('');
        try {
            const result = await api.listMcpTools(name, version);
            if (id === sequence.current)
                setInspection({ key, tools: result.tools });
        }
        catch (cause) {
            if (id === sequence.current)
                setError(cause instanceof Error ? cause.message : '工具清单读取失败。');
        }
        finally {
            if (id === sequence.current)
                setLoading('');
        }
    }
    if (!snapshot || !drafts)
        return <section aria-label="CTF 配置">{query.isError ? <p role="alert" className={controls.error}>CTF 配置读取失败。<Button className={controls.button} onClick={() => void query.refetch()} htmlType={"button"}>重试</Button></p> : <p role="status">正在读取 CTF 配置…</p>}</section>;
    const current = drafts[role];
    const updateTools = (tools: WorkerTools) => setDrafts({ ...drafts, [role]: { ...current, tools } });
    const selected = current.tools.mcp_servers ?? [];
    const available = [...(servers.data ?? []), ...selected.filter((bound) => !servers.data?.some((item) => item.name === bound.name && item.version === bound.version)).map((bound) => ({ ...bound, label: bound.name, enabled: false }))];
    return <section aria-label="CTF 角色配置">
    {error && <p className={styles.errorBanner} role="alert">{error} <Button onClick={() => void reload()} htmlType={"button"}>重新读取</Button></p>}
    {notice && <p className={styles.noticeBanner} role="status">{notice}</p>}
    <div className={styles.directWorker}><div className={styles.directActions}><Segmented aria-label="CTF 角色" value={role} disabled={saving} options={[{value:'lead',label:'Lead · 团队负责人'},{value:'teammate',label:'队友 · Teammate'}]} onChange={value=>{setRole(value as Role);setError('');setNotice('');setInspection(null);setLoading('');sequence.current+=1;}} /><div className={styles.headerActions}><span className={controls.hint}>新任务生效</span><Button size="small" type="primary" loading={saving} disabled={saving||!changed(role)&&!bindingsDirty} onClick={()=>void save()}>保存 CTF 配置</Button></div></div>
        <Form component={false} disabled={saving}><div className={styles.workerContent}>

          <div className={styles.workerForm}><section className={styles.promptColumn}><label className={controls.label} htmlFor={`ctf-prompt-${role}`}>{role === 'lead' ? 'Lead' : '队友'}完整系统提示词</label>
          <Input.TextArea id={`ctf-prompt-${role}`} className={controls.textarea} classNames={{textarea:styles.promptEditor}} styles={{textarea:{height:"42vh",minHeight:360}}} value={current.prompt} spellCheck={false} disabled={saving} onChange={(event) => setDrafts({ ...drafts, [role]: { ...current, prompt: event.target.value } })}/></section><aside className={styles.toolsColumn}>
          <fieldset className={styles.toolFieldset} disabled={saving}><legend>内置工具</legend><div className={styles.toolChecks}>{[...new Set([...builtin[role], ...current.tools.builtin])].map((name) => <label className={styles.checkRow} key={name}><Checkbox checked={current.tools.builtin.includes(name)} onChange={(event) => updateTools({ ...current.tools, builtin: event.target.checked ? [...current.tools.builtin, name] : current.tools.builtin.filter((item) => item !== name) })}/>{name}</label>)}</div></fieldset>
          <fieldset className={styles.toolFieldset} disabled={saving}><legend>外部 MCP 工具</legend>
            {servers.isError && <p className={controls.error} role="alert">MCP 服务读取失败。<Button onClick={() => void servers.refetch()} htmlType={"button"}>重试</Button></p>}
            {available.map((server) => {
            const bound = selected.find((item) => item.name === server.name && item.version === server.version);
            const key = `${server.name}@${server.version}`;
            return <div className={styles.mcpChoice} key={key}>
                <label className={styles.checkRow}><Checkbox checked={Boolean(bound)} disabled={saving || !server.enabled && !bound} onChange={(event) => updateTools({ ...current.tools, mcp_servers: event.target.checked ? [...selected.filter((item) => item.name !== server.name), { name: server.name, version: server.version, allowed_tools: [] }] : selected.filter((item) => item !== bound) })}/>{server.label} · v{server.version}{!server.enabled && <small>已停用</small>}</label>
                {bound && <><Button className={controls.button} disabled={saving || loading === key} onClick={() => void inspect(server.name, server.version)} htmlType={"button"}>{loading === key ? '正在读取…' : `读取 ${server.label} 工具清单`}</Button>
                  {inspection?.key === key && <div className={styles.toolChecks}>{inspection.tools.map((item) => <label className={styles.checkRow} key={item.name}><Checkbox checked={bound.allowed_tools == null || bound.allowed_tools.includes(item.name)} onChange={(event) => {
                                const allowed = bound.allowed_tools ?? inspection.tools.map((tool) => tool.name);
                                updateTools({ ...current.tools, mcp_servers: selected.map((entry) => entry === bound ? { ...entry, allowed_tools: event.target.checked ? [...new Set([...allowed, item.name])] : allowed.filter((name) => name !== item.name) } : entry) });
                            }}/>{item.name}<small>{item.description}</small></label>)}</div>}
                </>}
              </div>;
        })}
            {!servers.isLoading && !available.length && <p className={controls.hint}>请先在 MCP 工具页添加服务。</p>}
            <p className={controls.hint}>读取清单并选择具体工具，再为每个工具指定用途。只有已分类的允许工具会启用。</p>
          </fieldset>
          </aside></div><fieldset className={styles.toolFieldset} disabled={saving}><legend>已选平台工具用途</legend>
            <p className={controls.hint}>同一服务版本的工具用途在角色间共用。队友仅能使用提交和查询工具；模拟结果适配只用于测试平台。</p>
            {selected.flatMap((server) => (server.allowed_tools ?? []).map((name) => {
            const existing = bindings.find((item) => item.server_name === server.name && item.server_version === server.version && item.tool_name === name);
            const binding: Binding = existing ?? { server_name: server.name, server_version: server.version, tool_name: name, purpose: 'unknown', result_adapter: 'none', read_only: false };
            const update = (patch: Partial<Binding>) => setBindings((items) => [...items.filter((item) => !(item.server_name === server.name && item.server_version === server.version && item.tool_name === name)), { ...binding, ...patch }]);
            return <div className={styles.platformBinding} key={`${server.name}@${server.version}/${name}`}>
                <strong>{server.name}@{server.version} / {name}</strong>
                <label className={controls.field}><span className={controls.label}>用途</span><Select className={controls.select} aria-label={`${name} 用途`} value={binding.purpose} onChange={(value) => { const purpose = value as Binding['purpose']; update({ purpose, read_only: ['connect', 'status'].includes(purpose) && binding.read_only }); }} options={[{ value: "unknown", label: <>未分类（不启用）</> }, ...(role === 'lead' ? [{ value: "management", label: "靶机管理" }, { value: "connect", label: "连接信息" }] : []), { value: "submit", label: <>提交候选答案</> }, { value: "status", label: <>查询验证状态</> }]} disabled={saving} style={{ minWidth: 0, width: "100%" }}/></label>
                <label className={controls.field}><span className={controls.label}>结果适配</span><Select className={controls.select} aria-label={`${name} 结果适配`} value={binding.result_adapter} onChange={(value) => update({ result_adapter: value as Binding['result_adapter'] })} options={[{ value: "none", label: <>无适配（待核实）</> }, { value: "fake_ctf_v1", label: <>模拟 CTF v1（仅测试）</> }]} disabled={saving} style={{ minWidth: 0, width: "100%" }}/></label>
                <label className={styles.checkRow}><Checkbox checked={binding.read_only} disabled={saving || !['connect', 'status'].includes(binding.purpose)} onChange={(event) => update({ read_only: event.target.checked })}/>只读调用（仅连接或查询）</label>
              </div>;
        }))}
          </fieldset>
          <Collapse className={styles.variableHelp} size={"small"} ghost items={[{ key: "content", label: <>模板变量</>, children: <><p>{'{{ member_name }}'}、{'{{ member_id }}'}、{'{{ task_id }}'}、{'{{ agent_workspace }}'}、{'{{ shared_workspace }}'}、{'{{ allowed_tool_names }}'}</p><p>具体任务与用户插话由带来源的消息传入。</p></> }]}/>
        </div></Form>
      </div>
  </section>;
}

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, ApiError, type WorkerTools } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

type Role = 'lead' | 'teammate';
type Draft = { prompt: string; tools: WorkerTools };
type Settings = Awaited<ReturnType<typeof api.getCtfWorkerSettings>>;
type Binding = NonNullable<Settings['profile']['platform_tools']>[number];
const roles: Role[] = ['lead', 'teammate'];
const common = ['list_members', 'send_message', 'execute_command', 'list_challenges', 'get_challenge', 'create_challenge', 'update_challenge', 'list_records', 'append_record', 'register_artifact', 'request_help', 'record_candidate'];
const builtin = { lead: [...common, 'create_teammate', 'stop_teammate', 'resume_teammate', 'remove_teammate', 'finish_task', 'set_verification_required', 'confirm_messages'], teammate: common };
const from = (value: Settings, role: Role): Draft => ({ prompt: value.profile.prompt_templates[role], tools: value.profile.worker_tools[role] });

export default function CtfSettings({ onDirtyChange }: { onDirtyChange: (dirty: boolean) => void }) {
  const query = useQuery({ queryKey: ['ctf-worker-settings'], queryFn: api.getCtfWorkerSettings, refetchOnWindowFocus: false });
  const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers });
  const [snapshot, setSnapshot] = useState<Settings | null>(null);
  const [drafts, setDrafts] = useState<Record<Role, Draft> | null>(null);
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [role, setRole] = useState<Role>('lead');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [inspection, setInspection] = useState<{ key: string; tools: { name: string; description: string }[] } | null>(null);
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
    if (dirty && !window.confirm('放弃未保存的 CTF 配置并重新读取？')) return;
    const result = await query.refetch();
    if (result.data) {
      setSnapshot(result.data);
      setBindings(result.data.profile.platform_tools ?? []);
      setDrafts({ lead: from(result.data, 'lead'), teammate: from(result.data, 'teammate') });
      setError(''); setNotice('已重新读取。');
    }
  }
  async function save() {
    if (!snapshot || !drafts) return;
    for (const server of drafts[role].tools.mcp_servers ?? []) {
      if (server.allowed_tools == null) { setError('请读取 MCP 清单并逐项选择工具。'); return; }
      for (const name of server.allowed_tools) {
        const binding = bindings.find((item) => item.server_name === server.name && item.server_version === server.version && item.tool_name === name);
        if (!binding || binding.purpose === 'unknown') { setError(`请先为工具 ${name} 指定用途；未分类工具不会启用。`); return; }
      }
    }
    setSaving(true); setError(''); setNotice('');
    try {
      const saved = await api.saveCtfWorkerSettings(role, snapshot.revision, drafts[role].prompt, drafts[role].tools, bindings);
      setSnapshot(saved);
      setBindings(saved.profile.platform_tools ?? []);
      setDrafts((current) => current ? { ...current, [role]: from(saved, role) } : null);
      setNotice('CTF 配置已保存，仅用于之后创建的任务。');
    } catch (cause) {
      setError(cause instanceof ApiError && cause.status === 409 ? '配置已被其他窗口更新；当前草稿已保留，请复制后重新读取。' : cause instanceof Error ? cause.message : '保存失败。');
    } finally { setSaving(false); }
  }
  async function inspect(name: string, version: number) {
    const id = ++sequence.current;
    const key = `${name}@${version}`;
    setLoading(key); setInspection(null); setError('');
    try {
      const result = await api.listMcpTools(name, version);
      if (id === sequence.current) setInspection({ key, tools: result.tools });
    } catch (cause) { if (id === sequence.current) setError(cause instanceof Error ? cause.message : '工具清单读取失败。'); }
    finally { if (id === sequence.current) setLoading(''); }
  }
  if (!snapshot || !drafts) return <section aria-label="CTF 配置">{query.isError ? <p role="alert" className={controls.error}>CTF 配置读取失败。<button className={controls.button} onClick={() => void query.refetch()}>重试</button></p> : <p role="status">正在读取 CTF 配置…</p>}</section>;
  const current = drafts[role];
  const updateTools = (tools: WorkerTools) => setDrafts({ ...drafts, [role]: { ...current, tools } });
  const selected = current.tools.mcp_servers ?? [];
  const available = [...(servers.data ?? []), ...selected.filter((bound) => !servers.data?.some((item) => item.name === bound.name && item.version === bound.version)).map((bound) => ({ ...bound, label: bound.name, enabled: false }))];
  return <section aria-label="CTF 角色配置">
    <p className={styles.sectionHint}>Lead 和队友使用独立系统模板。提示词与工具配置在创建任务时保存快照，仅对新任务生效。</p>
    {error && <p className={styles.errorBanner} role="alert">{error} <button type="button" onClick={() => void reload()}>重新读取</button></p>}
    {notice && <p className={styles.noticeBanner} role="status">{notice}</p>}
    <div className={styles.workerLayout}>
      <nav className={styles.workerSidebar} aria-label="CTF 角色"><div className={styles.workerTabs}>{roles.map((item) => <button type="button" key={item} disabled={saving} aria-pressed={role === item} className={role === item ? styles.workerActive : styles.workerTab} onClick={() => { setRole(item); setError(''); setNotice(''); setInspection(null); setLoading(''); sequence.current += 1; }}><span className={styles.roleName}>{item === 'lead' ? 'Lead · 团队负责人' : '队友 · Teammate'}</span>{changed(item) && <small>未保存</small>}</button>)}</div></nav>
      <div className={styles.directWorker}>
        <div className={styles.directActions}><span className={controls.hint}>仅用于新任务 · 版本 {snapshot.revision}</span><button type="button" className={`${controls.button} ${controls.primary}`} disabled={saving || (!changed(role) && !bindingsDirty)} onClick={() => void save()}>{saving ? '正在保存…' : '保存 CTF 配置'}</button></div>
        <div className={styles.workerContent}>
          <label className={controls.label} htmlFor={`ctf-prompt-${role}`}>{role === 'lead' ? 'Lead' : '队友'}完整系统提示词</label>
          <textarea id={`ctf-prompt-${role}`} className={`${controls.textarea} ${styles.promptEditor}`} value={current.prompt} spellCheck={false} disabled={saving} onChange={(event) => setDrafts({ ...drafts, [role]: { ...current, prompt: event.target.value } })} />
          <fieldset className={styles.toolFieldset} disabled={saving}><legend>内置工具</legend><div className={styles.toolChecks}>{[...new Set([...builtin[role], ...current.tools.builtin])].map((name) => <label className={styles.checkRow} key={name}><input type="checkbox" checked={current.tools.builtin.includes(name)} onChange={(event) => updateTools({ ...current.tools, builtin: event.target.checked ? [...current.tools.builtin, name] : current.tools.builtin.filter((item) => item !== name) })} />{name}</label>)}</div></fieldset>
          <fieldset className={styles.toolFieldset} disabled={saving}><legend>外部 MCP 工具</legend>
            {servers.isError && <p className={controls.error} role="alert">MCP 服务读取失败。<button type="button" onClick={() => void servers.refetch()}>重试</button></p>}
            {available.map((server) => {
              const bound = selected.find((item) => item.name === server.name && item.version === server.version);
              const key = `${server.name}@${server.version}`;
              return <div className={styles.mcpChoice} key={key}>
                <label className={styles.checkRow}><input type="checkbox" checked={Boolean(bound)} disabled={!server.enabled && !bound} onChange={(event) => updateTools({ ...current.tools, mcp_servers: event.target.checked ? [...selected.filter((item) => item.name !== server.name), { name: server.name, version: server.version, allowed_tools: [] }] : selected.filter((item) => item !== bound) })} />{server.label} · v{server.version}{!server.enabled && <small>已停用</small>}</label>
                {bound && <><button type="button" className={controls.button} disabled={loading === key} onClick={() => void inspect(server.name, server.version)}>{loading === key ? '正在读取…' : `读取 ${server.label} 工具清单`}</button>
                  {inspection?.key === key && <div className={styles.toolChecks}>{inspection.tools.map((item) => <label className={styles.checkRow} key={item.name}><input type="checkbox" checked={bound.allowed_tools == null || bound.allowed_tools.includes(item.name)} onChange={(event) => {
                    const allowed = bound.allowed_tools ?? inspection.tools.map((tool) => tool.name);
                    updateTools({ ...current.tools, mcp_servers: selected.map((entry) => entry === bound ? { ...entry, allowed_tools: event.target.checked ? [...new Set([...allowed, item.name])] : allowed.filter((name) => name !== item.name) } : entry) });
                  }} />{item.name}<small>{item.description}</small></label>)}</div>}
                </>}
              </div>;
            })}
            {!servers.isLoading && !available.length && <p className={controls.hint}>请先在 MCP 工具页添加服务。</p>}
            <p className={controls.hint}>读取清单并选择具体工具，再为每个工具指定用途。只有已分类的允许工具会启用。</p>
          </fieldset>
          <fieldset className={styles.toolFieldset} disabled={saving}><legend>已选平台工具用途</legend>
            <p className={controls.hint}>同一服务版本的工具用途在角色间共用。队友仅能使用提交和查询工具；模拟结果适配只用于测试平台。</p>
            {selected.flatMap((server) => (server.allowed_tools ?? []).map((name) => {
              const existing = bindings.find((item) => item.server_name === server.name && item.server_version === server.version && item.tool_name === name);
              const binding: Binding = existing ?? { server_name: server.name, server_version: server.version, tool_name: name, purpose: 'unknown', result_adapter: 'none', read_only: false };
              const update = (patch: Partial<Binding>) => setBindings((items) => [...items.filter((item) => !(item.server_name === server.name && item.server_version === server.version && item.tool_name === name)), { ...binding, ...patch }]);
              return <div className={styles.platformBinding} key={`${server.name}@${server.version}/${name}`}>
                <strong>{server.name}@{server.version} / {name}</strong>
                <label className={controls.field}><span className={controls.label}>用途</span><select className={controls.select} aria-label={`${name} 用途`} value={binding.purpose} onChange={(event) => { const purpose = event.target.value as Binding['purpose']; update({ purpose, read_only: ['connect', 'status'].includes(purpose) && binding.read_only }); }}>
                  <option value="unknown">未分类（不启用）</option>
                  {role === 'lead' && <><option value="management">靶机管理</option><option value="connect">连接信息</option></>}
                  <option value="submit">提交候选答案</option><option value="status">查询验证状态</option>
                </select></label>
                <label className={controls.field}><span className={controls.label}>结果适配</span><select className={controls.select} aria-label={`${name} 结果适配`} value={binding.result_adapter} onChange={(event) => update({ result_adapter: event.target.value as Binding['result_adapter'] })}><option value="none">无适配（待核实）</option><option value="fake_ctf_v1">模拟 CTF v1（仅测试）</option></select></label>
                <label className={styles.checkRow}><input type="checkbox" checked={binding.read_only} disabled={!['connect', 'status'].includes(binding.purpose)} onChange={(event) => update({ read_only: event.target.checked })} />只读调用（仅连接或查询）</label>
              </div>;
            }))}
          </fieldset>
          <details className={styles.variableHelp}><summary>模板变量</summary><p>{'{{ member_name }}'}、{'{{ member_id }}'}、{'{{ task_id }}'}、{'{{ agent_workspace }}'}、{'{{ shared_workspace }}'}、{'{{ allowed_tool_names }}'}</p><p>具体任务与用户插话由带来源的消息传入。</p></details>
        </div>
      </div>
    </div>
  </section>;
}

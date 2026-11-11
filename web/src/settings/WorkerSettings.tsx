import { Alert, App, Button, Checkbox, Input } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError, type McpTool, type RuntimeInput, type WorkerRole, type WorkerSettings as Snapshot, type WorkerSettingsProfile, type WorkerTools } from '../api/client';
import LoadingState from '../components/LoadingState';
import McpGrantPicker from '../components/McpGrantPicker';
import AgentWorkbench from './AgentWorkbench';
import { promptVariables } from './agentRoles';
import PromptEditor from './PromptEditor';
import { useTrackSettings, type ChangeTracking } from './settingsChanges';
import ToolPermissions, { McpToolDrawer } from './ToolPermissions';
import type { RoleDirectory, CatalogQuery } from './toolCatalog';
import styles from './Settings.module.css';

const workerRoles: WorkerRole[] = ['explore', 'derive', 'close'];
const lifecycle = '配置仅对新建任务生效；已创建及续跑的任务沿用原快照。';
const paramsLabels: Partial<Record<keyof NonNullable<RuntimeInput['params']>, string>> = {
  explore_max_steps: '单次探索最多模型调用', seed_max_steps: '种子探索最大步数',
  close_reserve_ratio: '收尾预算预留比例', conclude_grace_calls: '结束后工具调用宽限次数',
  delta_max_lines: '增量推送最大行数', derive_empty_limit: '连续空推导上限',
  dispute_notify_depth: '争议通知最大深度', grace_timeout: '结束宽限（分钟）', heartbeat_timeout: '心跳超时（分钟）',
  intent_max_attempts: '意图最大尝试次数', max_consecutive_failures: '连续运行错误上限', snapshot_max_lines: 'YAML 快照最大行数',
};
type RoleDraft = { prompt: string; tools?: WorkerTools };
type Drafts = Record<WorkerRole, RoleDraft>;
const readDraft = (profile: WorkerSettingsProfile, role: WorkerRole) => ({
  prompt: profile.prompt_templates[role], tools: profile.worker_tools?.[role] ?? undefined,
});
const roleDrafts = (profile: WorkerSettingsProfile): Drafts => ({
  explore: readDraft(profile, 'explore'), derive: readDraft(profile, 'derive'), close: readDraft(profile, 'close'),
});
const readRuntime = (profile: WorkerSettingsProfile): RuntimeInput => ({ params: profile.params, exec_resources: profile.exec_resources });
const changed = (a: unknown, b: unknown) => JSON.stringify(a) !== JSON.stringify(b);
const defaultWorkerTools = (directory: RoleDirectory): WorkerTools => ({
  builtin: [...new Set(directory.tools.flatMap(tool => tool.setting_key ? [tool.setting_key] : []))],
  mcp_servers: [],
});
const errorText = (error: unknown) => error instanceof ApiError && error.status === 409
  ? '配置已在其他窗口更新。草稿已保留，可复制后重新读取。'
  : error instanceof Error ? error.message : '操作失败，请重试。';

function WorkerRuntime({ value, onChange, disabled }: { value: RuntimeInput; onChange: (next: RuntimeInput) => void; disabled: boolean }) {
  const params = value.params ?? {};
  const fields = Object.entries(paramsLabels) as [keyof typeof params, string][];
  return <>
    <div className={styles.panelHeading}><div><h3>运行设置</h3><p>全局调度适用于所有 Worker 角色；资源与网络边界由平台决定。</p></div></div>
    <section className={styles.formBlock}><h4>全局调度</h4>
      <fieldset className={styles.fields} disabled={disabled}>
        {fields.map(([key, label]) => params[key] == null ? null : <label key={key} className={styles.field}>
          <span>{label}</span><Input type="number" min={0} step={key === 'close_reserve_ratio' ? 'any' : '1'}
            value={String(params[key])} onChange={event => {
              if (event.target.value !== '') onChange({ ...value, params: { ...params, [key]: key === 'close_reserve_ratio' ? event.target.value : Number(event.target.value) } });
            }} /></label>)}
      </fieldset>
      <div className={styles.grantBanner}><div><strong>主动并行推导</strong><p>允许 Derive 在符合调度条件时主动补充方向。</p></div>
        <Checkbox aria-label="启用主动并行推导" disabled={disabled} checked={params.derive_enabled}
          onChange={event => onChange({ ...value, params: { ...params, derive_enabled: event.target.checked } })} /></div>
    </section>
    <section className={styles.formBlock}><h4>执行环境资源 <small>部署决定 · 只读</small></h4>
      <dl className={styles.values}>
        <div><dt>CPU、内存与进程数上限</dt><dd>由当前执行环境部署固定</dd></div><div><dt>交接上下文阈值</dt><dd>按任务模型有效输入的 80% 计算</dd></div>
      </dl>
    </section>
    <section className={styles.formBlock}><h4>网络与记录</h4><dl className={styles.values}>
      <div><dt>任务网络</dt><dd>未配置代理时可使用默认网络；代理配置后 Agent 不能修改或关闭。</dd></div>
      <div><dt>流量采集</dt><dd>按 Agent 采集</dd></div><div><dt>命令记录</dt><dd>仅记录与查看</dd></div>
    </dl></section>
    <p className={styles.note}><InfoCircleOutlined /> {lifecycle}</p>
  </>;
}

export default function WorkerSettings({ query, tracking, initialTool }: { query: CatalogQuery; tracking: ChangeTracking; initialTool?: 'mcp' }) {
  const { modal } = App.useApp(), client = useQueryClient();
  const settings = useQuery({ queryKey: ['worker-settings'], queryFn: api.getWorkerSettings, refetchOnWindowFocus: false });
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [drafts, setDrafts] = useState<Drafts | null>(null);
  const [runtime, setRuntime] = useState<RuntimeInput | null>(null);
  const [role, setRole] = useState<WorkerRole>('explore');
  const [saving, setSaving] = useState(false), [reloading, setReloading] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [externalDetail, setExternalDetail] = useState<{ tool: McpTool; source: string; scope: string } | null>(null);
  const inFlight = useRef(false);
  const current = drafts?.[role];
  const directory = query.isError ? undefined : query.data?.roles?.[role];
  const effectiveTools = current?.tools ?? (directory ? defaultWorkerTools(directory) : undefined);
  const dirtyRole = (key: WorkerRole) => Boolean(snapshot && drafts && changed(drafts[key], readDraft(snapshot.profile, key)));
  const runtimeDirty = Boolean(snapshot && runtime && changed(runtime, readRuntime(snapshot.profile)));
  const dirty = runtimeDirty || workerRoles.some(dirtyRole), busy = saving || reloading;
  useTrackSettings(dirty, busy, tracking);
  function load(next: Snapshot) {
    setSnapshot(next); setDrafts(roleDrafts(next.profile)); setRuntime(readRuntime(next.profile));
  }
  useEffect(() => { if (settings.data && !snapshot) load(settings.data); }, [settings.data, snapshot]);

  function updateRole(change: Partial<RoleDraft>) {
    setDrafts(old => old && { ...old, [role]: { ...old[role], ...change } });
    setNotice('');
  }
  async function save(runtimeOnly: boolean) {
    if (!snapshot || !runtime || !current || inFlight.current || (!runtimeOnly && (!directory || !effectiveTools))) return;
    if (!runtimeOnly && effectiveTools?.mcp_servers?.some(binding => binding.catalog_version === 'p6b' && !binding.allowed_tools?.length)) {
      setError('请为每个选中的 MCP 服务至少选择一个已发布工具，或取消选用。'); return;
    }
    inFlight.current = true; setSaving(true); setError(''); setNotice('');
    try {
      const next = runtimeOnly ? await api.saveRuntimeSettings(snapshot.revision, runtime)
        : await api.saveWorkerSettings(role, snapshot.revision, current.prompt, effectiveTools!);
      setSnapshot(next);
      client.setQueryData(['worker-settings'], next);
      if (runtimeOnly) setRuntime(readRuntime(next.profile));
      else setDrafts(old => old && { ...old, [role]: readDraft(next.profile, role) });
      setNotice(runtimeOnly ? '已保存全局调度。' : '已保存角色配置。');
    } catch (cause) { setError(errorText(cause)); }
    finally { inFlight.current = false; setSaving(false); }
  }
  async function reload() {
    if (inFlight.current) return;
    if (dirty && !await new Promise<boolean>(resolve => modal.confirm({
      title: '放弃草稿并重新读取？', okText: '放弃并读取', cancelText: '继续编辑', onOk: () => resolve(true), onCancel: () => resolve(false),
    }))) return;
    if (inFlight.current) return;
    inFlight.current = true; setReloading(true);
    try {
      const result = await settings.refetch({ throwOnError: true });
      if (result.data) { load(result.data); setError(''); setNotice('已重新读取。'); }
    } catch (cause) { setError(errorText(cause)); }
    finally { inFlight.current = false; setReloading(false); }
  }

  if (!snapshot || !runtime || !current) return settings.isError
    ? <Alert type="error" showIcon title="无法读取 Worker 配置" description={errorText(settings.error)} action={<Button onClick={() => void reload()}>重试</Button>} />
    : <LoadingState label="正在读取 Worker 配置" lines={7} />;

  return <>
    {error && <Alert className={styles.feedback} type="error" showIcon title={error} action={<Button disabled={busy} onClick={() => void reload()}>重新读取</Button>} />}
    {notice && <Alert className={styles.feedback} type="success" showIcon role="status" title={notice + lifecycle} />}
    <AgentWorkbench role={role} choices={workerRoles} onRole={next => { setRole(next as WorkerRole); setError(''); setNotice(''); }}
      intro="按角色管理通用任务的调查、推导和裁定规则。" note={lifecycle} query={query} busy={busy} badge={`版本 ${snapshot.revision}`}
      initialTool={initialTool} actions={panel => {
        const runtimeOnly = panel === 'runtime', pending = runtimeOnly ? runtimeDirty : dirtyRole(role);
        return <>{pending && <small className={styles.unsaved}>未保存</small>}<Button size="small" type="primary" loading={saving}
          disabled={busy || !pending || (!runtimeOnly && !directory)} onClick={() => void save(runtimeOnly)}>
          {runtimeOnly ? '保存全局设置' : '保存角色配置'}
        </Button></>;
      }}
      render={(panel, source) => panel === 'prompt' ? <PromptEditor role={role === 'explore' ? 'Explore' : role === 'derive' ? 'Derive' : 'Close'}
        value={current.prompt} variables={promptVariables[role]} onChange={prompt => updateRole({ prompt })} disabled={busy} />
        : panel === 'runtime' ? <WorkerRuntime value={runtime} disabled={busy} onChange={value => { setRuntime(value); setNotice(''); }} />
        : <ToolPermissions role={role} directory={directory} query={query} initial={source} enabled={effectiveTools?.builtin} disabled={busy}
          onChange={builtin => { if (effectiveTools) updateRole({ tools: { ...effectiveTools, builtin } }); }}
          external={role === 'explore' && effectiveTools && <>
            <p>服务发布后可按版本逐工具选用。发布新版本不会自动增加权限。</p>
            <McpGrantPicker role="explore" selected={effectiveTools.mcp_servers ?? []} disabled={busy}
              onChange={mcp_servers => updateRole({ tools: { ...effectiveTools, mcp_servers } })}
              onToolDetails={(tool, source) => setExternalDetail({ tool, source, scope: 'Explore 仅能使用此版本中明确选中的工具。' })} />
            <Link to="/mcp" className={styles.textLink}>前往 MCP 管理</Link>
          </>} />}
    />
    <McpToolDrawer details={externalDetail} onClose={() => setExternalDetail(null)} />
  </>;
}

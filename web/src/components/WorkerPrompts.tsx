import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import YAML from 'yaml';
import { api } from '../api/client';
import { parseProfileYaml, stringifyProfileYaml, type ProfileInput } from '../profiles/yaml';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

const workers = [
  { id: 'explore', title: 'Explore · 探索', description: '执行调查、提交事实和意图。完整模板包含种子启动与认领意图后的工作规则。' },
  { id: 'derive', title: 'derive · 推导', description: '根据已有事实与验收缺口提出新意图，包含并行推导和静止推导使用的完整规则。' },
  { id: 'close', title: 'Close · 裁定与终结', description: '核对证据、裁定验收并生成报告。完整模板包含 judge 裁定与 final 终结两个分支。' },
] as const;
type Role = (typeof workers)[number]['id'];
type ToolEntry = { builtin: string[]; mcp_servers: { name: string; version: number; allowed_tools: string[] | null }[] };
type EditableProfile = ProfileInput & { worker_tools?: Record<Role, ToolEntry> };
const builtin: Record<Role, string[]> = {
  explore: ['post_fact', 'post_intent', 'claim', 'release', 'get', 'search', 'read_evidence', 'execute_command'],
  derive: ['post_intent', 'get', 'search', 'read_evidence'],
  close: ['submit_close', 'get', 'search', 'read_evidence'],
};
const required: Record<Role, string[]> = {
  explore: ['post_fact', 'release'], derive: ['post_intent'], close: ['submit_close', 'get', 'read_evidence'],
};
const paramLabels: Record<string, string> = {
  close_reserve_ratio: '收尾预算预留比例', conclude_grace_calls: '结束后工具调用宽限次数',
  context_threshold: '交接上下文 token 阈值', delta_max_lines: '增量推送最大行数',
  derive_empty_limit: '连续空推导上限', dispute_notify_depth: '争议通知最大深度',
  explore_max_steps: '单次探索最多模型调用', grace_timeout: '结束宽限（分钟）',
  heartbeat_timeout: '心跳超时（分钟）', intent_max_attempts: '意图最大尝试次数',
  max_consecutive_failures: '连续运行错误上限', seed_max_steps: '种子探索最大步数',
  snapshot_max_lines: 'YAML 快照最大行数',
};

export default function WorkerPrompts({ source, onChange, disabled = false }: {
  source: string; onChange?: (value: string) => void; disabled?: boolean;
}) {
  const [worker, setWorker] = useState<Role>('explore');
  const [advanced, setAdvanced] = useState(false);
  const [notice, setNotice] = useState('');
  const [tools, setTools] = useState<{ name: string; description: string }[] | null>(null);
  const [toolError, setToolError] = useState('');
  const [inspectedServer, setInspectedServer] = useState('');
  const models = useQuery({ queryKey: ['platform-models'], queryFn: api.listPlatformModels, enabled: Boolean(onChange) });
  const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers, enabled: Boolean(onChange) });
  const profile = useMemo(() => {
    try { return YAML.parse(source, { uniqueKeys: true, maxAliasCount: 100 }) as EditableProfile; }
    catch { return null; }
  }, [source]);
  const current = workers.find((item) => item.id === worker)!;
  const prompt = profile?.prompt_templates?.[worker];
  const valid = typeof prompt === 'string';
  const model = profile?.models?.[worker];
  const platformModelId = model?.platform_id && model.platform_version ? `${model.platform_id}@${model.platform_version}` : '';
  const selection = profile?.worker_tools?.[worker];
  const selectedBuiltin = selection?.builtin ?? builtin[worker];
  const selectedServers = worker === 'explore' ? selection?.mcp_servers ?? [] : [];

  function update(patch: Partial<EditableProfile>) {
    if (profile && onChange) onChange(stringifyProfileYaml({ ...profile, ...patch }));
  }
  function updateTools(next: ToolEntry) {
    if (!profile) return;
    const current = profile.worker_tools ?? {
      explore: { builtin: builtin.explore, mcp_servers: [] },
      derive: { builtin: builtin.derive, mcp_servers: [] },
      close: { builtin: builtin.close, mcp_servers: [] },
    };
    update({ worker_tools: { ...current, [worker]: next } });
  }
  function changeBuiltin(name: string, checked: boolean) {
    const next = checked ? [...selectedBuiltin, name] : selectedBuiltin.filter((item) => item !== name);
    updateTools({ builtin: builtin[worker].filter((item) => next.includes(item)), mcp_servers: selection?.mcp_servers ?? [] });
  }
  function changeServer(name: string, version: number, checked: boolean) {
    const next = checked
      ? [...selectedServers.filter((item) => item.name !== name), { name, version, allowed_tools: null }]
      : selectedServers.filter((item) => item.name !== name || item.version !== version);
    updateTools({ builtin: selectedBuiltin, mcp_servers: next });
    setTools(null); setToolError(''); setInspectedServer('');
  }
  async function inspectTools(name: string, version: number) {
    setTools(null); setToolError(''); setInspectedServer(`${name}@${version}`);
    try { setTools((await api.listMcpTools(name, version)).tools); }
    catch (cause) { setToolError(cause instanceof Error ? cause.message : '工具读取失败。'); }
  }

  function showWorkers() {
    try { parseProfileYaml(source); setAdvanced(false); setNotice(''); }
    catch { setNotice('请先修正高级 YAML 中的格式或必填内容，草稿已保留。'); }
  }
  function changePrompt(value: string) {
    if (profile) update({ prompt_templates: { ...profile.prompt_templates, [worker]: value } });
  }
  async function copy() {
    try { await navigator.clipboard.writeText(prompt ?? ''); setNotice('完整提示词已复制。'); }
    catch { setNotice('复制失败，请选中正文手动复制。'); }
  }

  return <div className={styles.workerContent}>
    <div className={styles.tabs} role="group" aria-label="配置编辑方式">
      <button type="button" className={!advanced ? styles.tabActive : styles.tab} onClick={showWorkers}>Worker 配置</button>
      <button type="button" className={advanced ? styles.tabActive : styles.tab} onClick={() => { setAdvanced(true); setNotice(''); }}>高级 YAML</button>
    </div>
    {notice && <p role="status" className={controls.hint}>{notice}</p>}
    {advanced ? <>
      <label className={controls.label} htmlFor="profile-yaml">完整 AgentProfile YAML</label>
      <p className={controls.hint}>可同时修改模型价格、参数、执行环境与三个 worker 的提示词；与提示词编辑共享同一份草稿。</p>
      {onChange ? <textarea id="profile-yaml" className={`${controls.textarea} ${styles.yamlEditor}`} value={source} onChange={(event) => onChange(event.target.value)} spellCheck={false} disabled={disabled} /> : <pre className={styles.yamlView} tabIndex={0} aria-label="完整 AgentProfile YAML">{source}</pre>}
    </> : <>
      <div className={styles.workerTabs} role="group" aria-label="选择 worker">{workers.map((item) => <button type="button" key={item.id} aria-pressed={worker === item.id} onClick={() => { setWorker(item.id); setNotice(''); }} className={worker === item.id ? styles.workerActive : styles.workerTab}>{item.title}</button>)}</div>
      <div className={styles.workerHeading}><div><h3>{current.title}</h3><p>{current.description}</p></div><button type="button" className={controls.button} onClick={() => void copy()} disabled={!valid}>复制全文</button></div>
      {model && <p className={styles.modelSummary}>{model.model} · 推理 {model.reasoning_effort} · 计价 {model.price?.currency === 'CNY' ? '人民币 CNY' : model.price?.currency ?? '未配置'}</p>}
      {onChange && model && <label className={controls.field}><span className={controls.label}>平台模型</span><select className={controls.select} value={platformModelId} onChange={(event) => {
        const selected = models.data?.find((item) => `${item.name}@${item.version}` === event.target.value);
        if (selected && profile) update({ models: { ...profile.models, [worker]: selected.config } });
      }}><option value="">当前版本的自定义模型</option>{platformModelId && !models.data?.some((item) => `${item.name}@${item.version}` === platformModelId) && <option value={platformModelId}>{model.platform_id} · v{model.platform_version}（已固定）</option>}{models.data?.filter((item) => item.enabled || `${item.name}@${item.version}` === platformModelId).map((item) => <option key={`${item.name}@${item.version}`} value={`${item.name}@${item.version}`}>{item.label} · v{item.version}</option>)}</select><span className={controls.hint}>选择时复制平台模型的固定版本；后续平台修改不会改变本配置。</span></label>}
      <label className={controls.label} htmlFor={`prompt-${worker}`}>完整系统提示词 · {worker}</label>
      {!valid ? <p role="alert">提示词结构不完整，请切换高级 YAML 修正。草稿不会丢失。</p> : onChange ? <textarea id={`prompt-${worker}`} className={`${controls.textarea} ${styles.promptEditor}`} value={prompt} onChange={(event) => changePrompt(event.target.value)} spellCheck={false} disabled={disabled} aria-describedby="prompt-help" /> : <pre id={`prompt-${worker}`} className={styles.promptView} tabIndex={0}>{prompt}</pre>}
      <p id="prompt-help" className={controls.hint}>这里是该版本完整的系统提示词模板。目标、黑板、验收状态等变量在运行时注入；实际初始上下文可在任务的 Agent 对话中查看。</p>
      {onChange && profile && <>
        <fieldset className={styles.toolFieldset}><legend>内置工具</legend><div className={styles.toolChecks}>{builtin[worker].map((name) => <label key={name} className={styles.checkRow}><input type="checkbox" checked={selectedBuiltin.includes(name)} disabled={disabled || required[worker].includes(name)} onChange={(event) => changeBuiltin(name, event.target.checked)} />{name}{required[worker].includes(name) && <small>必需</small>}</label>)}</div></fieldset>
        {worker === 'explore' && <fieldset className={styles.toolFieldset}><legend>Explore 外部 MCP</legend>
          {(servers.data?.length || selectedServers.length) ? [...(servers.data ?? []), ...selectedServers.filter((bound) => !servers.data?.some((item) => item.name === bound.name && item.version === bound.version)).map((bound) => ({ name: bound.name, version: bound.version, label: bound.name, enabled: false, historical: true }))].map((server) => {
            const bound = selectedServers.find((item) => item.name === server.name && item.version === server.version);
            const key = `${server.name}@${server.version}`;
            return <div key={key} className={styles.mcpChoice}>
              <label className={styles.checkRow}><input type="checkbox" checked={Boolean(bound)} disabled={disabled || (!server.enabled && !bound)} onChange={(event) => changeServer(server.name, server.version, event.target.checked)} />{server.label} · v{server.version}{'historical' in server ? <small>已固定历史版本</small> : !server.enabled && <small>已停用</small>}</label>
              {bound && <><button type="button" className={controls.button} onClick={() => void inspectTools(server.name, server.version)}>读取 {server.label} 工具清单</button>
                {inspectedServer === key && toolError && <p className={controls.error} role="alert">{toolError}</p>}
                {inspectedServer === key && tools && <div className={styles.toolChecks}>{tools.map((tool) => <label key={tool.name} className={styles.checkRow}><input type="checkbox" checked={bound.allowed_tools === null || bound.allowed_tools.includes(tool.name)} onChange={(event) => {
                  const current = bound.allowed_tools ?? tools.map((item) => item.name);
                  const allowed = event.target.checked ? [...current, tool.name] : current.filter((item) => item !== tool.name);
                  updateTools({ builtin: selectedBuiltin, mcp_servers: selectedServers.map((item) => item === bound ? { ...item, allowed_tools: allowed } : item) });
                }} />{tool.name}<small>{tool.description}</small></label>)}</div>}
              </>}
            </div>;
          }) : <p className={controls.hint}>平台尚未添加 MCP 服务。</p>}
          <p className={controls.hint}>未限制时允许该服务全部工具；取消勾选后只允许选中的工具。外部工具仅在 Explore 阶段可用。</p>
        </fieldset>}
        <details className={styles.settingsDetails}><summary>调度参数与执行环境</summary><div className={styles.formGrid}>
          {Object.entries(profile.params).map(([key, value]) => key === 'derive_enabled' ? <label key={key} className={styles.checkRow}><input type="checkbox" checked={Boolean(value)} onChange={(event) => update({ params: { ...profile.params, derive_enabled: event.target.checked } })} />启用推导（含并行）</label> : typeof value === 'number' || typeof value === 'string' && key === 'close_reserve_ratio' ? <label className={controls.field} key={key}><span className={controls.label}>{paramLabels[key] ?? key}</span><input className={controls.input} type="number" min="0" step={key === 'close_reserve_ratio' ? 'any' : '1'} value={value} onChange={(event) => update({ params: { ...profile.params, [key]: Number(event.target.value) } })} /></label> : null)}
          <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>执行镜像</span><input className={controls.input} value={profile.exec_image} onChange={(event) => update({ exec_image: event.target.value })} /></label>
          <label className={controls.field}><span className={controls.label}>CPU</span><input className={controls.input} type="number" min="0.1" step="any" value={profile.exec_resources.cpus} onChange={(event) => update({ exec_resources: { ...profile.exec_resources, cpus: Number(event.target.value) } })} /></label>
          <label className={controls.field}><span className={controls.label}>内存</span><input className={controls.input} value={profile.exec_resources.mem} onChange={(event) => update({ exec_resources: { ...profile.exec_resources, mem: event.target.value } })} /></label>
          <label className={controls.field}><span className={controls.label}>进程上限</span><input className={controls.input} type="number" min="1" step="1" value={profile.exec_resources.pids} onChange={(event) => update({ exec_resources: { ...profile.exec_resources, pids: Number(event.target.value) } })} /></label>
          <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>提权命令前缀（每行一个）</span><textarea className={controls.textarea} value={profile.privileged_allowlist?.join('\n') ?? ''} onChange={(event) => update({ privileged_allowlist: event.target.value.split('\n').map((item) => item.trim()).filter(Boolean) })} /></label>
        </div></details>
      </>}
      <details className={styles.variableHelp}><summary>模板变量与分支说明</summary><p>通用变量：{'{{ goal }}'}、{'{{ domain_context }}'}、{'{{ acceptance_status }}'}、{'{{ agent_id }}'}。</p><p>Explore：current_intent、yaml_snapshot、seed_max_steps、conclude_grace_calls；derive：facts_text、intents_text、previous_excluded；Close：mode、satisfies_facts、judgment_history、yaml_snapshot。</p><p>保留所需的 Jinja 变量和条件分支。种子复用 Explore，judge/final 复用 Close，无需另建 worker。</p></details>
    </>}
  </div>;
}

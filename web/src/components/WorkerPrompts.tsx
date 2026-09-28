import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type WorkerRole, type WorkerTools } from '../api/client';
import { builtin, required, workers } from './workerOptions';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

export default function WorkerPrompts({ role, prompt, tools, onPromptChange, onToolsChange, disabled }: {
  role: WorkerRole; prompt: string; tools: WorkerTools;
  onPromptChange: (value: string) => void; onToolsChange: (value: WorkerTools) => void; disabled: boolean;
}) {
  const [inspected, setInspected] = useState('');
  const [toolError, setToolError] = useState('');
  const [toolList, setToolList] = useState<{ name: string; description: string }[] | null>(null);
  const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers, enabled: role === 'explore' });
  const info = workers.find((item) => item.id === role)!;
  const selected = tools.mcp_servers ?? [];
  function changeServer(name: string, version: number, checked: boolean) {
    onToolsChange({ ...tools, mcp_servers: checked
      ? [...selected.filter((entry) => entry.name !== name), { name, version, allowed_tools: null }]
      : selected.filter((entry) => entry.name !== name || entry.version !== version) });
    setInspected(''); setToolList(null); setToolError('');
  }
  async function inspect(name: string, version: number) {
    setInspected(`${name}@${version}`); setToolList(null); setToolError('');
    try { setToolList((await api.listMcpTools(name, version)).tools); }
    catch (cause) { setToolError(cause instanceof Error ? cause.message : '工具读取失败。'); }
  }
  return <div className={styles.workerContent}>
    <div className={styles.workerHeading}><div><h2>{info.title}</h2><p>{info.description}</p></div></div>
    <label className={controls.label} htmlFor={`prompt-${role}`}>完整系统提示词</label>
    <textarea id={`prompt-${role}`} className={`${controls.textarea} ${styles.promptEditor}`} value={prompt} onChange={(event) => onPromptChange(event.target.value)} spellCheck={false} disabled={disabled} aria-describedby="prompt-help" />
    <p id="prompt-help" className={controls.hint}>保存后的提示词会在运行中 Agent 的下一次模型调用前，使用当前任务上下文重新渲染。工具设置仅对新任务生效。</p>
    <fieldset className={styles.toolFieldset}><legend>内置工具</legend><div className={styles.toolChecks}>{builtin[role].map((name) => <label key={name} className={styles.checkRow}><input type="checkbox" checked={tools.builtin.includes(name)} disabled={disabled || required[role].includes(name)} onChange={(event) => onToolsChange({ ...tools, builtin: event.target.checked ? [...tools.builtin, name] : tools.builtin.filter((item) => item !== name) })} />{name}{required[role].includes(name) && <small>必需</small>}</label>)}</div></fieldset>
    {role === 'explore' && <fieldset className={styles.toolFieldset}><legend>外部 MCP 工具</legend>
      {(servers.data?.length || selected.length) ? [...(servers.data ?? []), ...selected.filter((bound) => !servers.data?.some((item) => item.name === bound.name && item.version === bound.version)).map((bound) => ({ name: bound.name, version: bound.version, label: bound.name, enabled: false }))].map((server) => {
        const bound = selected.find((entry) => entry.name === server.name && entry.version === server.version);
        const key = `${server.name}@${server.version}`;
        return <div key={key} className={styles.mcpChoice}>
          <label className={styles.checkRow}><input type="checkbox" checked={Boolean(bound)} disabled={disabled || (!server.enabled && !bound)} onChange={(event) => changeServer(server.name, server.version, event.target.checked)} />{server.label}{!server.enabled && <small>已停用</small>}</label>
          {bound && <><button type="button" className={controls.button} onClick={() => void inspect(server.name, server.version)}>读取 {server.label} 工具清单</button>
            {inspected === key && toolError && <p className={controls.error} role="alert">{toolError}</p>}
            {inspected === key && toolList && <div className={styles.toolChecks}>{toolList.map((tool) => <label key={tool.name} className={styles.checkRow}><input type="checkbox" checked={bound.allowed_tools == null || bound.allowed_tools.includes(tool.name)} disabled={disabled} onChange={(event) => {
              const current = bound.allowed_tools ?? toolList.map((item) => item.name);
              const allowed = event.target.checked ? [...current, tool.name] : current.filter((item) => item !== tool.name);
              onToolsChange({ ...tools, mcp_servers: selected.map((entry) => entry === bound ? { ...entry, allowed_tools: allowed } : entry) });
            }} />{tool.name}<small>{tool.description}</small></label>)}</div>}
          </>}
        </div>;
      }) : <p className={controls.hint}>平台尚未添加 MCP 服务。</p>}
      <p className={controls.hint}>未限制时允许该服务全部工具；取消勾选后只允许选中的工具。</p>
    </fieldset>}
    <details className={styles.variableHelp}><summary>模板变量与分支说明</summary><p>通用变量：{'{{ goal }}'}、{'{{ domain_context }}'}、{'{{ acceptance_status }}'}、{'{{ agent_id }}'}。</p><p>Explore：current_intent、yaml_snapshot、seed_max_steps、conclude_grace_calls；Derive：facts_text、intents_text、previous_excluded；Close：mode、satisfies_facts、judgment_history、yaml_snapshot。</p></details>
  </div>;
}

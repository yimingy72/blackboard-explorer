import { Alert, Button, Empty } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type McpRole, type McpTool, type SettingsRole } from '../api/client';
import LoadingState from '../components/LoadingState';
import { usePublishedMcpServers } from '../components/mcpGrants';
import AgentWorkbench from '../settings/AgentWorkbench';
import { promptVariables } from '../settings/agentRoles';
import PromptEditor from '../settings/PromptEditor';
import ToolPermissions, { McpToolDrawer } from '../settings/ToolPermissions';
import type { CatalogQuery } from '../settings/toolCatalog';
import styles from '../settings/Settings.module.css';

function PublishedExternalTools({ role }: { role: McpRole }) {
  const { query, servers } = usePublishedMcpServers(role);
  const [loading, setLoading] = useState(false), [error, setError] = useState('');
  const [list, setList] = useState<{ source: string; tools: McpTool[] } | null>(null);
  const [detail, setDetail] = useState<{ tool: McpTool; source: string; scope: string } | null>(null);
  const sequence = useRef(0);
  async function inspect(name: string, version: number, source: string) {
    const id = ++sequence.current;
    setLoading(true); setError(''); setList(null);
    try {
      const result = await api.listMcpTools(name, version);
      if (id === sequence.current) {
        if (!result.published || !result.roles?.includes(role)) setError('此版本未发布给当前角色。');
        else setList({ source, tools: result.tools });
      }
    } catch (cause) { if (id === sequence.current) setError(cause instanceof Error ? cause.message : '无法读取工具清单。'); }
    finally { if (id === sequence.current) setLoading(false); }
  }
  return <>
    <p>发布只决定可选范围。请在新建原生 CTF 任务时为角色逐工具明确授权；Helper 不继承。</p>
    {query.isLoading ? <LoadingState label="正在读取已发布的 MCP 服务" /> : query.isError
      ? <Alert type="warning" title="无法读取此角色的 MCP 发布范围" action={<Button onClick={() => void query.refetch()}>重试</Button>} />
      : servers.length ? <ul className={styles.externalList}>{servers.map(server =>
        <li key={server.name}><strong>{server.label} · v{server.version}</strong><Button size="small" disabled={loading}
          onClick={() => void inspect(server.name, server.version, server.label + ' · v' + server.version)}>查看工具</Button></li>)}</ul>
        : <p>此角色没有已发布的外部 MCP 服务。</p>}
    {loading && <p role="status">正在读取工具清单…</p>}
    {error && <Alert type="error" title={error} />}
    {list && <div className={styles.actions}>{list.tools.map(tool => <Button size="small" key={tool.name}
      onClick={() => setDetail({ tool, source: list.source, scope: '仅在新建 CTF 任务时为此角色明确授权后可用。' })}>{tool.name}</Button>)}</div>}
    <Link to="/mcp" className={styles.textLink}>前往 MCP 管理</Link>
    <McpToolDrawer details={detail} onClose={() => setDetail(null)} />
  </>;
}

export default function CtfSettings({ query }: { query: CatalogQuery }) {
  const published = useQuery({ queryKey: ['published-ctf-settings'], queryFn: api.getCtfWorkerSettings });
  const capabilities = useQuery({ queryKey: ['task-creation-capabilities'], queryFn: api.taskCreationCapabilities, enabled: published.data?.published === true });
  const [role, setRole] = useState<'lead' | 'teammate'>('lead');
  if (published.isLoading) return <LoadingState label="正在读取已发布的 CTF 角色" />;
  if (published.isError) return <Alert type="error" title="无法读取原生 CTF 发布" action={<Button onClick={() => void published.refetch()}>重试</Button>} />;
  const value = published.data;
  if (!value?.published) return <div className={styles.emptyPanel}><Empty description="当前没有已发布的原生 CTF 角色" />
    <p>角色和工具由部署管理员发布。历史任务仍可查看。</p></div>;
  const allowExternal = capabilities.data?.native_ctf?.external_mcp === true;
  const entry = query.isError ? undefined : query.data?.roles?.[role];
  const directory = entry && { ...entry, external_mcp: entry.external_mcp && allowExternal };
  return <AgentWorkbench role={role} choices={['lead', 'teammate']} onRole={(key: SettingsRole) => setRole(key as typeof role)}
    intro="查看原生 CTF 的角色分工、发布边界与运行约束。" note="已发布角色配置只读。新建任务使用当前发布版本；已创建的任务保留原快照。"
    badge={'发布版本 ' + value.revision + ' · 只读'} query={query} allowExternal={allowExternal}
    render={(panel, source) => panel === 'prompt' ? <PromptEditor role={role === 'lead' ? 'CTF Lead' : 'CTF Worker'}
      value={value.prompt_templates[role]} variables={promptVariables[role]} />
      : panel === 'tools' ? <><ToolPermissions role={role} directory={directory} query={query} initial={source}
        external={<PublishedExternalTools role={role} />} />
        <p className={styles.note}><InfoCircleOutlined /> Helper 不作为独立配置角色；不继承平台黑板和外部 MCP 权限。</p></>
      : <><div className={styles.panelHeading}><div><h3>运行设置</h3><p>任务网络、执行与记录范围由部署发布决定。</p></div></div>
        <section className={styles.formBlock}><h4>任务网络</h4><dl className={styles.values}>
          <div><dt>网络能力</dt><dd>{value.task_network ? '默认网络或任务共享 VPN' : '当前发布未开放任务网络选择'}</dd></div>
          <div><dt>配置范围</dt><dd>{role === 'lead' ? '网络能力开放时，Lead 按任务需要选择。' : '继承任务网络，由 Lead 管理共享 VPN。'}</dd></div>
        </dl></section>
        <section className={styles.formBlock}><h4>执行记录</h4><dl className={styles.values}>
          <div><dt>流量采集</dt><dd>按 Agent 采集</dd></div><div><dt>命令记录</dt><dd>仅记录与查看</dd></div>
        </dl></section></>}
  />;
}

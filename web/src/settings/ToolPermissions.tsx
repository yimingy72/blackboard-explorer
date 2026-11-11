import { ApiOutlined, ApartmentOutlined, CodeOutlined, LockOutlined } from '@ant-design/icons';
import { Alert, Button, Drawer, Switch, Tag } from 'antd';
import { useState, type ReactNode } from 'react';
import type { McpTool, SettingsRole, SettingsTool } from '../api/client';
import LoadingState from '../components/LoadingState';
import { roles } from './agentRoles';
import styles from './Settings.module.css';

import { sources, type CatalogQuery, type RoleDirectory, type ToolSource } from './toolCatalog';

const sourceIcons = { sandbox: <CodeOutlined />, platform: <ApartmentOutlined />, mcp: <ApiOutlined /> };
const accessLabels = { read: '只读', write: '写入', execute: '执行' };
const labels: Record<string, string> = {
  exec_command: '运行命令', read_file: '读取文件', write_file: '写入文件',
  execution_status: '查询执行状态', execution_output: '读取执行输出', interrupt_execution: '中断执行',
  get: '读取对象', search: '搜索资料', read_evidence: '读取证据', post_fact: '登记事实',
  post_intent: '登记意图', claim: '认领意图', release: '释放意图', submit_close: '提交裁定',
  board_read: '读取题目黑板', board_search: '搜索题目黑板', board_challenges: '查看题目列表',
  board_announcements: '查看任务公告', board_flags: '查看候选记录', board_flag_value: '读取候选值',
  board_publish_announcement: '发布任务公告', board_create_challenge: '建立题目',
  board_dispatch_challenge: '调整题目调度', board_update_challenge: '更新题目信息',
  board_submit_flag: '提交平台 Flag', board_finish_task: '结束任务', board_post_fact: '登记题目事实',
  board_post_intent: '登记题目意图', board_record_flag: '登记 Flag 候选',
  vpn_configure: '配置任务 VPN', vpn_disconnect: '断开任务 VPN', vpn_status: '查看 VPN 状态', network_use_default: '选择默认网络',
  read_progress_sources: '读取资料增量', get_progress_tree: '读取候选成果树', update_progress_tree: '维护候选成果树',
};
const samples: Record<string, object> = {
  exec_command: { command: 'pwd', cwd: '/workspace', timeout_seconds: 10 },
  read_file: { path: '/workspace/notes.txt', limit: 4096 },
  write_file: { path: '/workspace/notes.txt', content: '整理任务摘要。' },
  get: { object_id: 'F-001' }, search: { q: '任务简报' },
  read_progress_sources: { after_version: 0, limit: 30 }, get_progress_tree: {},
};
const toolLabel = (tool: SettingsTool) => labels[tool.name] ?? tool.name;
const descriptions: Record<string, string> = {
  exec_command: '在当前任务的执行沙箱中运行命令。', read_file: '读取当前工作目录中的文件。', write_file: '在获准工作目录中写入文件。',
  execution_status: '查看已发起命令的状态。', execution_output: '读取已发起命令的输出。', interrupt_execution: '请求终止已发起的命令。',
  get: '读取已知对象的正文或状态。', search: '按关键词查找任务资料。', read_evidence: '回读当前任务登记的证据。',
  post_fact: '登记有证据支撑的观察。', post_intent: '登记可验证的后续方向。', claim: '认领当前任务的可用意图。',
  release: '释放意图并记录处理状态。', submit_close: '提交终结判断或报告。',
  read_progress_sources: '分页读取任务来源增量。', get_progress_tree: '读取候选成果树及其版本。', update_progress_tree: '增量维护候选成果树。',
  board_read: '读取角色获准查看的题目知识。', board_search: '按关键词查找获准访问的题目资料。', board_challenges: '查看角色权限内的题目状态。',
  board_announcements: '读取任务公告及更新。', board_flags: '读取题目的候选记录。', board_flag_value: '读取获准查看的候选值。',
  board_publish_announcement: '发布任务范围或状态的变更。', board_create_challenge: '为当前任务登记题目。', board_dispatch_challenge: '更新题目可调度或撤回状态。',
  board_update_challenge: '更新题目标题或说明。', board_submit_flag: '由 Lead 使用获准的候选记录发起提交。', board_finish_task: '在获准收尾时记录任务结论。',
  board_post_fact: '登记分配题目的事实。', board_post_intent: '登记分配题目的后续方向。', board_record_flag: '记录分配题目的候选与依据。',
  vpn_configure: '按任务需要配置已开放的共享 VPN。', vpn_disconnect: '请求断开本任务的共享 VPN。',
  vpn_status: '读取本任务共享 VPN 状态。', network_use_default: '按任务说明选择默认网络。',
};
const toolDescription = (tool: SettingsTool) => descriptions[tool.name] || tool.description;

function scopeFor(role: SettingsRole, tool: SettingsTool) {
  if (tool.group === 'sandbox') return '仅限当前任务获准的沙箱与工作目录。';
  if (role === 'progress') return tool.access === 'write' ? '仅写入当前任务的候选成果树。' : '只读当前任务在来源版本范围内的资料。';
  if (tool.name.startsWith('vpn_') || tool.name === 'network_use_default') return '仅限本任务与部署发布的网络能力。';
  if (tool.name === 'board_announcements') return '当前任务内角色可读的共享公告。';
  if (role === 'teammate') return '限获准读取或写入的题目范围。';
  return '仅限当前任务内角色获准读取或写入的对象。';
}

export function SourceIcon({ source }: { source: ToolSource }) {
  return <span className={styles.sourceIcon} aria-hidden="true">{sourceIcons[source]}</span>;
}
export function McpToolDrawer({ details, onClose }: { details: { tool: McpTool; source: string; scope: string } | null; onClose: () => void }) {
  return <Drawer title={details?.tool.name || 'MCP 工具详情'} open={Boolean(details)} onClose={onClose} size="large">
    {details && <div className={styles.toolDrawer}>
      <p>{details.tool.description || '服务未提供用途说明。'}</p>
      <dl><div><dt>来源</dt><dd>{details.source}</dd></div><div><dt>授权范围</dt><dd>{details.scope}</dd></div></dl>
      <h3>输入 Schema</h3><pre>{JSON.stringify(details.tool.input_schema ?? {}, null, 2)}</pre>
      {details.tool.output_schema && <details className={styles.help}><summary>输出 Schema</summary><pre>{JSON.stringify(details.tool.output_schema, null, 2)}</pre></details>}
      {details.tool.annotations && <details className={styles.help}><summary>服务声明</summary>
        <p>声明由服务提供；任务权限以平台授权为准。</p><pre>{JSON.stringify(details.tool.annotations, null, 2)}</pre>
      </details>}
    </div>}
  </Drawer>;
}

export default function ToolPermissions({ role, directory, query, enabled, onChange, disabled, initial, external }: {
  role: SettingsRole; directory?: RoleDirectory; query: CatalogQuery; enabled?: string[];
  onChange?: (value: string[]) => void; disabled?: boolean; initial?: ToolSource; external?: ReactNode;
}) {
  const [detail, setDetail] = useState<SettingsTool | null>(null);
  const [selectedSource, setOpen] = useState<ToolSource | null | undefined>(initial);
  const open = selectedSource === undefined ? directory?.tools.some(tool => tool.group === 'sandbox') ? 'sandbox' : 'platform' : selectedSource;
  const sandbox = directory?.tools.filter(tool => tool.group === 'sandbox') ?? [];
  const platform = directory?.tools.filter(tool => tool.group === 'platform') ?? [];
  const sharedKeys = new Set(sandbox.filter(tool => tool.configurable).map(tool => tool.setting_key).filter((key): key is string => Boolean(key)));
  const grouped = sandbox.length > 1 && sharedKeys.size === 1 ? [...sharedKeys][0] : undefined;
  function toggle(key: string, checked: boolean) {
    const current = enabled ?? [];
    onChange?.(checked ? [...current, key] : current.filter(item => item !== key));
  }
  return <>
    <div className={styles.panelHeading}><div><h3>工具权限</h3><p>按来源查看能力、授权范围和参数。点击工具名称查看详情。</p></div></div>
    {query.isLoading && <LoadingState label="正在读取工具目录" />}
    {(query.isError || (!directory && !query.isLoading)) && <Alert showIcon type="error" title="未能读取此角色的工具目录"
      description="草稿仍保留。重新读取目录后才能保存此角色的配置。"
      action={<Button onClick={() => void query.refetch()}>重新读取</Button>} />}
    {!query.isError && directory && <div className={styles.toolGroups}>
      {(Object.keys(sources) as ToolSource[]).map(source => {
        const entries = source === 'sandbox' ? sandbox : source === 'platform' ? platform : [];
        const info = sources[source];
        return <section key={source} className={styles.toolGroup}>
          <button type="button" className={styles.sourceRow} aria-expanded={open === source} onClick={() => setOpen(open === source ? null : source)}>
            <SourceIcon source={source} /><span className={styles.sourceLabel}><strong>{info.title}</strong><small>{info.detail}</small></span>
            <span className={styles.sourceCount}>{source === 'mcp' ? directory.external_mcp ? '按发布版本选用' : '不适用' : entries.length ? entries.length + ' 项能力' : '不提供此能力'}</span>
            <span className={styles.chevron} aria-hidden="true">⌄</span>
          </button>
          {open === source && (source === 'mcp' ? <div className={styles.externalPane}>{directory.external_mcp ? external : '此角色没有外部 MCP 权限。'}</div>
            : entries.length ? <div>
              {source === 'sandbox' && grouped && <div className={styles.grantBanner}><div><strong>执行环境整组授权</strong><p>命令执行、文件读取和写入统一授权，对新建任务生效。</p></div>
                <Switch aria-label="执行环境整组授权" disabled={disabled} checked={enabled?.includes(grouped)} onChange={value => toggle(grouped, value)} /></div>}
              {entries.map(tool => <div key={tool.name} className={styles.toolRow}>
                <button className={styles.toolLink} type="button" onClick={() => setDetail(tool)}><strong>{toolLabel(tool)}</strong><code>{tool.name}</code></button>
                <span className={styles.toolDescription}>{toolDescription(tool)}</span>
                <Tag color={tool.access === 'read' ? undefined : 'processing'}>{accessLabels[tool.access]}</Tag>
                <div className={styles.permission}>
                  {tool.required ? <span><LockOutlined /> 系统必需</span>
                    : !tool.configurable || !tool.setting_key || !onChange ? <span><LockOutlined /> 部署固定</span>
                    : tool.setting_key === grouped ? <span>随整组授权</span>
                    : <Switch aria-label={'允许' + toolLabel(tool)} disabled={disabled} checked={enabled?.includes(tool.setting_key)}
                        onChange={value => toggle(tool.setting_key!, value)} />}
                </div>
              </div>)}
            </div> : <p className={styles.externalPane}>此角色没有{info.title}工具。</p>)}
        </section>;
      })}
    </div>}
    <Drawer title={detail ? toolLabel(detail) : '工具详情'} open={Boolean(detail)} onClose={() => setDetail(null)} size="large">
      {detail && <div className={styles.toolDrawer}>
        <p><code>{detail.name}</code> <Tag color={detail.access === 'read' ? undefined : 'processing'}>{accessLabels[detail.access]}</Tag></p>
        {toolDescription(detail) && <p>{toolDescription(detail)}</p>}
        <dl><div><dt>来源</dt><dd>{sources[detail.group].title} · {sources[detail.group].detail}</dd></div>
          <div><dt>适用角色</dt><dd>{(Object.entries(query.data?.roles ?? {}) as [SettingsRole, RoleDirectory][])
            .filter(([, entry]) => entry.tools.some(tool => tool.name === detail.name && tool.group === detail.group))
            .map(([key]) => key === 'lead' || key === 'teammate' ? 'CTF ' + roles[key].title : roles[key].title).join('、')}</dd></div>
          <div><dt>授权范围</dt><dd>{scopeFor(role, detail)}</dd></div></dl>
        {samples[detail.name] && <><h3>参数 JSON 示例</h3><pre>{JSON.stringify(samples[detail.name], null, 2)}</pre></>}
        <details className={styles.help} open={!samples[detail.name]}><summary>完整输入 Schema</summary><pre>{JSON.stringify(detail.input_schema, null, 2)}</pre></details>
      </div>}
    </Drawer>
  </>;
}

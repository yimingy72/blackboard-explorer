import { CheckOutlined, InfoCircleOutlined, SafetyOutlined } from '@ant-design/icons';
import { Tag } from 'antd';
import { useState, type ReactNode } from 'react';
import type { SettingsRole } from '../api/client';
import { roles } from './agentRoles';
import { DetailHeading, SettingsFrame, SettingsRail } from './SettingsShell';
import { SourceIcon } from './ToolPermissions';
import { sources, type CatalogQuery, type RoleDirectory, type ToolSource } from './toolCatalog';
import styles from './Settings.module.css';

const tabs = [['overview', '职责与能力'], ['prompt', '系统提示词'], ['tools', '工具权限'], ['runtime', '运行设置']] as const;
export type AgentPanel = typeof tabs[number][0];

function Overview({ role, directory, query, openSource, note }: {
  role: SettingsRole; directory?: RoleDirectory; query: CatalogQuery; openSource: (source: ToolSource) => void; note: string;
}) {
  const item = roles[role];
  return <>
    <div className={styles.overview}>
      <section><h3>核心职责</h3><ul className={styles.duties}>{item.work.map(([title, description]) =>
        <li key={title}><span aria-hidden="true"><CheckOutlined /></span><div><strong>{title}</strong><p>{description}</p></div></li>)}</ul></section>
      <aside className={styles.scope}><h3><SafetyOutlined /> 权限边界</h3><dl>{item.limits.map(([label, detail]) =>
        <div key={label}><dt>{label}</dt><dd>{detail}</dd></div>)}</dl></aside>
    </div>
    <section className={styles.capabilities}><h3>能力来源</h3>
      {(Object.keys(sources) as ToolSource[]).map(source => {
        const item = sources[source];
        const count = directory?.tools.filter(tool => tool.group === source).length ?? 0;
        const state = query.isError ? '读取失败' : !directory ? query.isLoading ? '读取中' : '暂无目录'
          : source === 'mcp' ? directory.external_mcp ? '按发布版本选用' : '不适用'
          : count ? `${count} 项能力` : '不提供此能力';
        return <button type="button" className={styles.sourceRow} key={source} onClick={() => openSource(source)}>
          <SourceIcon source={source} /><span className={styles.sourceLabel}><strong>{item.title}</strong><small>{item.detail}</small></span>
          <span className={styles.sourceCount}>{state}</span><span aria-hidden="true">›</span>
        </button>;
      })}
    </section>
    <div className={styles.note}><InfoCircleOutlined /><span>{note}</span></div>
  </>;
}

export default function AgentWorkbench({ role, choices, intro, note, onRole, busy, badge, query, actions, render, initialTool, allowExternal }: {
  role: SettingsRole; choices: SettingsRole[]; intro: string; note: string; onRole: (role: SettingsRole) => void;
  busy?: boolean; badge?: string; query: CatalogQuery; actions?: (panel: AgentPanel) => ReactNode;
  render: (panel: Exclude<AgentPanel, 'overview'>, source?: ToolSource) => ReactNode; initialTool?: ToolSource; allowExternal?: boolean;
}) {
  const item = roles[role], entry = query.data?.roles?.[role];
  const directory = entry && { ...entry, external_mcp: entry.external_mcp && allowExternal !== false };
  const [panel, setPanel] = useState<AgentPanel>(initialTool ? 'tools' : 'overview');
  const [source, setSource] = useState<ToolSource | undefined>(initialTool);
  return <SettingsFrame>
    <SettingsRail title="角色" intro={intro} items={choices.map(key => ({ id: key, ...roles[key] }))} active={role}
      note={<><InfoCircleOutlined /> {note}</>} disabled={busy} onSelect={key => { if (key !== role) { setSource(undefined); onRole(key as SettingsRole); } }} />
    <section className={styles.main} aria-label={item.title + ' 角色配置'}>
      <DetailHeading title={item.title} subtitle={item.subtitle} icon={item.icon} description={item.description}
        actions={badge ? <Tag color="default">{badge}</Tag> : undefined} />
      <div className={styles.subnavWrap}>
        <nav className={styles.subnav} aria-label="角色配置分区">{tabs.map(([key, label]) =>
          <button key={key} type="button" aria-pressed={panel === key} onClick={() => { setSource(undefined); setPanel(key); }}>{label}</button>)}</nav>
        {actions && <div className={styles.saveActions}>{actions(panel)}</div>}
      </div>
      <div className={styles.body} key={role + panel}>
        {panel === 'overview' ? <Overview role={role} directory={directory} query={query} note={note}
          openSource={value => { setSource(value); setPanel('tools'); }} /> : render(panel, source)}
      </div>
    </section>
  </SettingsFrame>;
}

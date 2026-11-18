import { Alert, Button, Checkbox } from 'antd';
import { useRef, useState } from 'react';
import { api, type McpRole, type McpTool } from '../api/client';
import { usePublishedMcpServers, type McpGrant } from './mcpGrants';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

export default function McpGrantPicker({ role, selected, onChange, disabled, onToolDetails }: {
    role: McpRole;
    selected: McpGrant[];
    onChange: (value: McpGrant[]) => void;
    disabled: boolean;
    onToolDetails?: (tool: McpTool, source: string) => void;
}) {
    const [inspected, setInspected] = useState('');
    const [toolError, setToolError] = useState('');
    const [toolList, setToolList] = useState<McpTool[] | null>(null);
    const [loadingTools, setLoadingTools] = useState(false);
    const inspectSequence = useRef(0);
    const { query, servers } = usePublishedMcpServers(role);
    const choices = [...servers, ...selected.filter(bound =>
        !servers.some(server => server.name === bound.name && server.version === bound.version)
    ).map(bound => ({
        name: bound.name, version: bound.version,
        label: query.data?.find(server => server.name === bound.name)?.label ?? bound.name, available: false,
    }))];

    function changeServer(name: string, version: number, checked: boolean) {
        onChange(checked
            ? [...selected.filter(entry => entry.name !== name), { name, version, catalog_version: 'p6b', allowed_tools: [] }]
            : selected.filter(entry => entry.name !== name || entry.version !== version));
        inspectSequence.current += 1;
        setInspected('');
        setToolList(null);
        setToolError('');
        setLoadingTools(false);
    }

    async function inspect(name: string, version: number) {
        const sequence = ++inspectSequence.current;
        setInspected(`${name}@${version}`);
        setToolList(null);
        setToolError('');
        setLoadingTools(true);
        try {
            const result = await api.listMcpTools(name, version);
            if (inspectSequence.current === sequence) {
                if (!result.published || !result.roles?.includes(role)) {
                    setToolError('此版本未授权给当前角色，不能选择工具。');
                } else setToolList(result.tools);
            }
        } catch (cause) {
            if (inspectSequence.current === sequence) setToolError(cause instanceof Error ? cause.message : '工具读取失败。');
        } finally {
            if (inspectSequence.current === sequence) setLoadingTools(false);
        }
    }

    return <div>
        {query.isError && <Alert type="warning" title="无法读取已发布的 MCP 服务" action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} />}
        {query.isLoading && <p className={controls.hint}>正在读取已发布服务…</p>}
        {!query.isLoading && !query.isError && !choices.length && <p className={controls.hint}>此角色没有已发布的 MCP 服务。</p>}
        {choices.map(server => {
            const bound = selected.find(entry => entry.name === server.name && entry.version === server.version);
            const key = `${server.name}@${server.version}`;
            const busy = inspected === key && loadingTools;
            return <div key={key} className={styles.mcpChoice}>
                <label className={styles.checkRow}><Checkbox
                    aria-label={server.available ? undefined : `${server.label} v${server.version} 原固定版本`}
                    checked={Boolean(bound)} disabled={disabled || (!server.available && !bound)}
                    onChange={event => changeServer(server.name, server.version, event.target.checked)}
                />{server.label}</label>
                <small className={controls.hint}>v{server.version}{!server.available ? ' · 原固定版本' : ' · 已发布'}</small>
                {bound && <>
                    <Button className={controls.button} htmlType="button" loading={busy} aria-busy={busy}
                        disabled={disabled || busy} onClick={() => void inspect(server.name, server.version)}>
                        { `读取 ${server.label} 工具清单` }
                    </Button>
                    {inspected === key && toolError && <p className={controls.error} role="alert">{toolError}</p>}
                    {inspected === key && toolList && <div className={styles.toolChecks}>
                        {toolList.map(tool => <div key={tool.name}>
                            <label className={styles.checkRow}><Checkbox checked={bound.allowed_tools?.includes(tool.name) === true} disabled={disabled} onChange={event => {
                                const current = bound.allowed_tools ?? [];
                                const allowed_tools = event.target.checked ? [...current, tool.name] : current.filter(item => item !== tool.name);
                                onChange(selected.map(entry => entry === bound ? { ...entry, allowed_tools } : entry));
                            }} />{tool.name}<small>{tool.description}</small></label>
                            {onToolDetails && <Button type="link" size="small" onClick={() => onToolDetails(tool, server.label + ' · v' + server.version)}>
                              查看 {tool.name} 参数
                            </Button>}
                        </div>)}
                    </div>}
                </>}
            </div>;
        })}
        {selected.length > 0 && <p className={controls.hint}>只授权明确选中的工具；发布新版本不会自动增加权限。</p>}
    </div>;
}

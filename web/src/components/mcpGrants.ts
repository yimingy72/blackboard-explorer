import { useQuery } from '@tanstack/react-query';
import { api, type McpRole, type WorkerTools } from '../api/client';

export const MCP_ROLE_LABELS: Record<McpRole, string> = {
    explore: '普通探索 Worker', lead: 'CTF Lead', teammate: 'CTF Worker',
};

export type McpGrant = NonNullable<WorkerTools['mcp_servers']>[number];

export function usePublishedMcpServers(role: McpRole) {
    const query = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers });
    const servers = (query.data ?? []).flatMap(server =>
        server.enabled && server.published_version && server.published_roles?.includes(role)
            ? [{ ...server, version: server.published_version, available: true }]
            : []);
    return { query, servers };
}

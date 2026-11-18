import { useQuery } from '@tanstack/react-query';
import { api, type SettingsRole, type SettingsToolCatalog } from '../api/client';

export const sources = {
  sandbox: { title: '执行环境', detail: 'Agent Runtime / OpenSandbox' },
  platform: { title: '平台黑板', detail: '内置业务工具' },
  mcp: { title: '外部 MCP', detail: '独立工具来源' },
};
export type ToolSource = keyof typeof sources;
export type RoleDirectory = NonNullable<SettingsToolCatalog['roles'][SettingsRole]>;

export function useToolCatalog() {
  // The result is read by lazy role panels. Notify the parent even if a panel
  // has not mounted by the time the directory finishes loading.
  return useQuery({ queryKey: ['settings-tool-catalog'], queryFn: () => api.getSettingsToolCatalog(),
    notifyOnChangeProps: ['data', 'error', 'isLoading', 'isError'], refetchOnWindowFocus: false });
}
export type CatalogQuery = ReturnType<typeof useToolCatalog>;

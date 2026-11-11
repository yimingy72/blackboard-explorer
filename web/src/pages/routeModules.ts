const pages = {
  tasks: () => import('./TasksPage'),
  create: () => import('./NewTaskPage'),
  workbench: () => import('./TaskWorkbenchPage'),
  profiles: () => import('./ProfilesPage'),
  models: () => import('../settings/ModelsPage'),
  mcp: () => import('../settings/McpPage'),
  report: () => import('./ReportPage'),
};
export const routeModules = {
  tasks: async () => ({ Component: (await pages.tasks()).default }),
  create: async () => ({ Component: (await pages.create()).default }),
  workbench: async () => ({ Component: (await pages.workbench()).default }),
  profiles: async () => ({ Component: (await pages.profiles()).default }),
  models: async () => ({ Component: (await pages.models()).default }),
  mcp: async () => ({ Component: (await pages.mcp()).default }),
  report: async () => ({ Component: (await pages.report()).default }),
};

export function preloadPage(pathname: string): void {
  const load = pathname === '/tasks' ? pages.tasks
    : pathname === '/tasks/new' ? pages.create
    : pathname === '/profiles' ? pages.profiles
    : pathname === '/models' ? pages.models
    : pathname === '/mcp' ? pages.mcp
    : /^\/tasks\/[^/]+\/report$/.test(pathname) ? pages.report
    : /^\/tasks\/[^/]+(?:\/progress)?$/.test(pathname) ? pages.workbench : undefined;
  // A failed speculative load is retried by the router when the user navigates.
  void load?.().catch(() => undefined);
}

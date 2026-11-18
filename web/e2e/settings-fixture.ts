const sandbox = (name: string, access: string) => ({
  name, group: 'sandbox', access, required: false, configurable: true, setting_key: 'execute_command',
  input_schema: { type: 'object', properties: { path: { type: 'string' } } }, description: null,
});
const platform = (name: string, access: string, required = false, readOnly = false) => ({
  name, group: 'platform', access, required, configurable: !required && !readOnly, setting_key: readOnly ? null : name,
  input_schema: { type: 'object' }, description: null,
});
const boardReads = [platform('get', 'read'), platform('search', 'read'), platform('read_evidence', 'read')];
export const fixtureToolCatalog = {
  roles: {
    explore: { external_mcp: true, tools: [...boardReads, platform('post_fact', 'write', true), platform('post_intent', 'write'),
      platform('claim', 'write'), platform('release', 'write', true), sandbox('exec_command', 'execute'), sandbox('read_file', 'read'), sandbox('write_file', 'write')] },
    derive: { external_mcp: false, tools: [...boardReads, platform('post_intent', 'write', true)] },
    close: { external_mcp: false, tools: [...boardReads, platform('submit_close', 'write', true)] },
    lead: { external_mcp: true, tools: [platform('board_announcements', 'read', false, true), platform('board_finish_task', 'write', false, true)] },
    teammate: { external_mcp: true, tools: [platform('board_announcements', 'read', false, true), platform('board_post_fact', 'write', false, true)] },
    progress: { external_mcp: false, tools: [...boardReads, platform('read_progress_sources', 'read', true), platform('get_progress_tree', 'read', true), platform('update_progress_tree', 'write', true)] },
  },
};

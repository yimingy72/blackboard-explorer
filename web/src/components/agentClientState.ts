/** Client intent state lasts for the task view, including inspector remounts. */
export type AgentClientState = {
  draftRevision: number;
  message: { id: string; content: string } | null;
  actions: Record<string, string>;
};
export const createAgentClientState = (): AgentClientState => ({draftRevision:0,message:null,actions:{}});

// Only the frozen Task scope supplies a destination or credential.
export const mcpAudiences = ["business-mcp", "sandbox-mcp"];

export function mcpScope(spec, target) {
  if (!["business", "sandbox"].includes(target) || !Object.hasOwn(spec.mcp_target_credential_refs, target)) {
    throw new Error("The MCP target is outside the frozen role");
  }
  return { audience: `${target}-mcp`, reference: spec.mcp_target_credential_refs[target] };
}

export function mcpScopes(spec) {
  return Object.keys(spec.mcp_target_credential_refs).map((target) => mcpScope(spec, target));
}

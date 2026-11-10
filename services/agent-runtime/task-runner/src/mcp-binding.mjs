// Only frozen Task scope supplies a destination or credential.
export const mcpAudiences = ["mcp", "business-mcp", "sandbox-mcp"];

export function mcpScope(spec, target) {
  if (spec.mcp_target_credential_refs !== undefined) {
    if (!["business", "sandbox"].includes(target) || !Object.hasOwn(spec.mcp_target_credential_refs, target)) {
      throw new Error("The MCP target is outside the frozen role");
    }
    return { audience: `${target}-mcp`, reference: spec.mcp_target_credential_refs[target] };
  }
  if (!spec.mcp_credential_ref) throw new Error("The original MCP credential is unavailable");
  return { audience: "mcp", reference: spec.mcp_credential_ref };
}

export function mcpScopes(spec) {
  return spec.mcp_target_credential_refs === undefined
    ? [mcpScope(spec)]
    : Object.keys(spec.mcp_target_credential_refs).map((target) => mcpScope(spec, target));
}

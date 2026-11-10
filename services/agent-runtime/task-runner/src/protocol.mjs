import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { Check } from "typebox/value";
import { isDeepStrictEqual } from "node:util";

export const PROTOCOL = "bbx.task-runner.v1";
export const SUPPORTED_BUSINESS_CONTRACTS = Object.freeze(["challenge-board-v1"]);
const schema = JSON.parse(await readFile(new URL("../protocol.schema.json", import.meta.url), "utf8"));
export function jsonText(value) {
  const text = JSON.stringify(value, (_key, value) => {
    if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Runner JSON requires finite numbers");
    return value;
  });
  if (typeof text !== "string") throw new Error("A JSON representation is required");
  return text;
}
export function digest(value) {
  if (!(typeof value === "string" || Buffer.isBuffer(value))) throw new Error("Runner digests require exact bytes or preserved JSON text");
  if (typeof value === "string" && Buffer.from(value, "utf8").toString("utf8") !== value) throw new Error("Runner text must be valid UTF-8");
  return createHash("sha256").update(value).digest("hex");
}
export function validate(name, value) {
  const invalid = () => Object.assign(new Error(`Invalid ${name}`), { code: "invalid_protocol" });
  if (!Check({ $ref: `#/$defs/${name}`, $defs: schema.$defs }, value)) throw invalid();
  if (name === "RunnerBootstrap" && !Object.keys(value.mcp_targets ?? {}).length) throw invalid();
  if (name === "RunnerPayload" && value.kind === "prepare" &&
      !(value.spec.mcp_tool_routes && Object.keys(value.spec.mcp_target_credential_refs ?? {}).length)) throw invalid();
  return value;
}
export const sameValue = isDeepStrictEqual;
export const sameFence = isDeepStrictEqual;
export function decodeCommand(command) {
  try {
    validate("RunnerCommand", command);
    if (digest(command.payload_json) !== command.payload_digest) throw new Error("Control payload bytes do not match their digest");
    const payload = validate("RunnerPayload", JSON.parse(command.payload_json));
    if (payload.kind === "input") {
      digest(payload.content);
      if (Buffer.byteLength(jsonText(payload.content), "utf8") > 131072) throw new Error("Input JSON exceeds the encoded byte bound");
    }
    return { ...command, payload };
  } catch (error) {
    throw Object.assign(error, { code: "invalid_protocol" });
  }
}
export function safeUrl(value) {
  const url = new URL(value);
  if (url.username || url.password || url.hash || url.search || !(url.protocol === "https:"
    || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) throw new Error("A trusted service URL is required");
  return url;
}

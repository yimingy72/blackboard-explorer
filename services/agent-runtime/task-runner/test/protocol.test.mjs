import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { decodeCommand, digest, jsonText, PROTOCOL } from "../src/protocol.mjs";
import { root } from "./support.mjs";

test("Python and Node verify preserved UTF-8 JSON text without reserializing numeric keys, decimals or Unicode", () => {
  const value = { "10": 1.0, "2": 1e-7, text: "你好 é 😀", nested: [-0.0, 1e20] };
  const nodeText = jsonText(value);
  const script = `import json,sys
from bbx_contracts.runner import runner_json_text, runner_payload_digest
data=json.load(sys.stdin)
assert runner_payload_digest(data['node_text'])==data['node_digest']
value={'10':1.0,'2':1e-7,'text':'你好 é 😀','nested':[-0.0,1e20]}
text=runner_json_text(value)
print(json.dumps({'python_text':text,'python_digest':runner_payload_digest(text)},ensure_ascii=False))
`;
  const result = spawnSync("uv", ["run", "--no-sync", "python", "-c", script], {
    cwd: resolve(root, "../../.."), encoding: "utf8", input: JSON.stringify({ node_text: nodeText, node_digest: digest(nodeText) }),
    env: { ...process.env, UV_CACHE_DIR: "/private/tmp/bbx-uv-cache" },
  });
  assert.equal(result.status, 0, result.stderr);
  const python = JSON.parse(result.stdout);
  assert.equal(digest(python.python_text), python.python_digest);
  const decoded = JSON.parse(python.python_text);
  assert.equal(decoded.text, value.text);
  assert.equal(decoded["2"], value["2"]);
  assert.equal(decoded["10"], value["10"]);
  assert.ok(decoded.nested[0] === value.nested[0]);
  assert.notEqual(python.python_text, nodeText);
  assert.ok(python.python_text.includes("1.0") && python.python_text.includes("1e-07"));
  assert.ok(nodeText.indexOf('"2"') < nodeText.indexOf('"10"'));
  const payload = jsonText({ kind: "input", binding_id: "binding", input_id: "input", content: "\u0001".repeat(20000), mode: "follow_up" });
  const command = { protocol: PROTOCOL, request_id: "request", command_seq: 1,
    fence: { task_id: "task", runner_id: "runner", pod_uid: "pod", epoch: 1 }, payload_json: payload, payload_digest: digest(payload) };
  assert.equal(decodeCommand(command).payload.content.length, 20000);
  assert.throws(() => decodeCommand({ ...command, payload_digest: "0".repeat(64) }), /do not match their digest/);
  const oversized = jsonText({ ...JSON.parse(payload), content: "\u0001".repeat(20001) });
  assert.throws(() => decodeCommand({ ...command, payload_json: oversized, payload_digest: digest(oversized) }), /Invalid RunnerPayload/);
});

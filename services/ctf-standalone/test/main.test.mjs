import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const entrypoint = fileURLToPath(new URL("../src/main.mjs", import.meta.url));

test("CLI help exits successfully without configuration or model requests", () => {
  const result = spawnSync(process.execPath, [entrypoint, "--help"], { encoding: "utf8", env: {}, timeout: 10000 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Usage: node src\/main\.mjs --config/);
  assert.match(result.stdout, /CTF_CONFIG_PATH/);
});

test("CLI missing explicit configuration exits with an actionable error", () => {
  const result = spawnSync(process.execPath, [entrypoint], { encoding: "utf8", env: {}, timeout: 10000 });
  assert.ifError(result.error);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Pass --config with the explicit local model configuration file/);
});

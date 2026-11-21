import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";
import test from "node:test";
import { loadConfig, parseArenaHelp } from "../src/config.mjs";

const root = fileURLToPath(new URL("../../../.data/ctf-standalone-tests/", import.meta.url));
const help = "# Scenario\n- Attempt: attempt-fixture\nAttempt window: this attempt expires at 2026-11-22T03:00:00Z; closes afterwards.\nARENA_SOLVER_BASE_URL=http://127.0.0.1:12345\nBENCHMARK_BASE_URL=http://127.0.0.1:12345 # alias\n";
const model = {
  provider: "fixture", id: "fixture-model", api: "openai-completions",
  baseUrl: "http://127.0.0.1:12346/v1", apiKeyEnv: "TEST_MODEL_KEY",
  contextWindow: 32768, maxTokens: 1024, reasoning: false,
};

async function fixture(t, config = { model }, environment = {}) {
  await mkdir(root, { recursive: true });
  const directory = await mkdtemp(join(root, "arena-config-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const configPath = join(directory, "config.json");
  const helpPath = join(directory, "help.md");
  await writeFile(configPath, JSON.stringify(config));
  return {
    configPath, helpPath,
    env: { ARENA_SOLVER_TOKEN: "fake-arena-token", TEST_MODEL_KEY: "fake-model-key", CYBER_ARENA_HELP_MARKDOWN: help, ...environment },
  };
}

test("loads only explicit model configuration and current injected attempt identity", async (t) => {
  const options = await fixture(t);
  const loaded = await loadConfig(options);
  assert.equal(loaded.workDir, "/workspace/ctf-run");
  assert.equal(loaded.arena.baseUrl, "http://127.0.0.1:12345");
  assert.equal(loaded.arena.attemptId, "attempt-fixture");
  assert.equal(loaded.arena.mode, "attempt");
  assert.equal(loaded.arena.expiresAt, Date.parse("2026-11-22T03:00:00Z"));
  assert.equal(loaded.model.apiKey, "fake-model-key");
  assert.equal(loaded.arena.token, "fake-arena-token");
  assert.equal(JSON.stringify(loaded).includes("fake-model-key"), false);
  assert.equal(JSON.stringify(loaded).includes("fake-arena-token"), false);
  assert.match(loaded.arena.identity, /^[a-f0-9]{64}$/);
});

test("reads the injected help file when the documented help environment is absent", async (t) => {
  const options = await fixture(t, { model, workDir: ".data/local-attempt" }, { CYBER_ARENA_HELP_MARKDOWN: "" });
  await writeFile(options.helpPath, help);
  const loaded = await loadConfig(options);
  assert.equal(loaded.workDir, resolve(".data/local-attempt"));
  assert.equal(loaded.arena.attemptId, "attempt-fixture");
});

test("same-attempt identity survives token rotation and changes for a new attempt", async (t) => {
  const options = await fixture(t);
  const first = await loadConfig(options);
  assert.equal((await loadConfig(options)).arena.identity, first.arena.identity);
  options.env.ARENA_SOLVER_TOKEN = "different-fake-token";
  assert.equal((await loadConfig(options)).arena.identity, first.arena.identity);
  options.env.CYBER_ARENA_HELP_MARKDOWN = help.replace("Attempt: attempt-fixture", "Attempt: another-attempt");
  assert.notEqual((await loadConfig(options)).arena.identity, first.arena.identity);
});

test("explicit local arena configuration supports a mock with no injected help", async (t) => {
  const options = await fixture(t, { model, arena: { baseUrl: "http://127.0.0.1:12345", tokenEnv: "LOCAL_TOKEN" } }, {
    CYBER_ARENA_HELP_MARKDOWN: "", LOCAL_TOKEN: "local-fixture-token",
  });
  const loaded = await loadConfig(options);
  assert.equal(loaded.arena.attemptId, null);
  assert.equal(loaded.arena.expiresAt, null);
  assert.equal(loaded.arena.token, "local-fixture-token");
  options.env.LOCAL_TOKEN = "changed-local-fixture-token";
  assert.notEqual((await loadConfig(options)).arena.identity, loaded.arena.identity);
});

test("conflicting rendered or explicit endpoints fail before any request", async (t) => {
  const options = await fixture(t, { model, arena: { baseUrl: "http://127.0.0.1:23456" } });
  await assert.rejects(loadConfig(options), /conflicts/);
  assert.throws(() => parseArenaHelp(help.replace("BENCHMARK_BASE_URL=http://127.0.0.1:12345", "BENCHMARK_BASE_URL=http://127.0.0.1:23456")), /conflicting/);
});

test("help parser handles markdown escapes and rejects an unrendered template", () => {
  assert.equal(parseArenaHelp(help.replaceAll("_", "\\_")).baseUrl, "http://127.0.0.1:12345");
  assert.equal(
    parseArenaHelp("Attempt: attempt-fixture\nAttempt expires at: 2026-11-22T03:00:00Z\n").expiresAt,
    Date.parse("2026-11-22T03:00:00Z"),
  );
  assert.equal(
    parseArenaHelp("Attempt: attempt-fixture\nAttempt expires at: **2026-11-22T03:00:00Z.**\n").expiresAt,
    Date.parse("2026-11-22T03:00:00Z"),
  );
  assert.throws(() => parseArenaHelp("ARENA_SOLVER_BASE_URL=http://model-proxy-<workload>.<namespace>:8090"), /URL/);
  assert.throws(() => parseArenaHelp("Attempt window: this attempt expires at invalid-date;"), /expiration/);
});

test("injected endpoint variables fill descriptive help and must agree", async (t) => {
  const descriptive = "# Cyber Arena — Challenges API\nRendered for a single attempt.\n"
    + "The API root is ARENA_SOLVER_BASE_URL; append /openapi/v1/ to it.\n";
  const options = await fixture(t, { model }, {
    CYBER_ARENA_HELP_MARKDOWN: descriptive,
    ARENA_SOLVER_BASE_URL: "http://127.0.0.1:12345",
    BENCHMARK_BASE_URL: "http://127.0.0.1:12345",
    ARENA_ATTEMPT_ID: "attempt-from-environment",
  });
  const loaded = await loadConfig(options);
  assert.equal(loaded.arena.baseUrl, "http://127.0.0.1:12345");
  assert.equal(loaded.arena.attemptId, "attempt-from-environment");

  options.env.BENCHMARK_BASE_URL = "http://127.0.0.1:23456";
  await assert.rejects(loadConfig(options), /Injected arena base URLs conflict/);
});

test("mode follows the official bootstrap preamble and never infers from task text", async (t) => {
  const probe = "# Cyber Arena — Challenges API\nRendered for a connectivity check. Do not cache or reuse it.\n" + help;
  const options = await fixture(t, { model }, { CYBER_ARENA_HELP_MARKDOWN: probe });
  assert.equal((await loadConfig(options)).arena.mode, "connectivity_test");
  assert.equal(parseArenaHelp("# Cyber Arena — Challenges API\nRendered for a single attempt.\n" + help).mode, "attempt");
  assert.equal(parseArenaHelp(help + "Challenge title: connectivity_test\nRendered for a connectivity check.\n").mode, null);
  assert.equal(parseArenaHelp("## Quoted handout\n# Cyber Arena — Challenges API\nRendered for a connectivity check.\n").mode, null);
});

test("explicit connectivity mode supports local fixtures and conflicting help is rejected", async (t) => {
  const options = await fixture(t, { model, arena: { mode: "connectivity_test" } });
  assert.equal((await loadConfig(options)).arena.mode, "connectivity_test");
  options.env.CYBER_ARENA_HELP_MARKDOWN = "# Cyber Arena — Challenges API\nRendered for a single attempt.\n" + help;
  await assert.rejects(loadConfig(options), /mode conflicts/);
  const invalid = await fixture(t, { model, arena: { mode: "probe" } });
  await assert.rejects(loadConfig(invalid), /Arena mode/);
});

test("help retained for agents is redacted", async (t) => {
  const options = await fixture(t, { model }, {
    CYBER_ARENA_HELP_MARKDOWN: help + "BENCHMARK_TOKEN=fake-arena-token\nExample fake-model-key\n",
  });
  const loaded = await loadConfig(options);
  assert.equal(loaded.arena.helpMarkdown.includes("fake-arena-token"), false);
  assert.equal(loaded.arena.helpMarkdown.includes("fake-model-key"), false);
});

test("missing credentials fail with a field name and never expose supplied values", async (t) => {
  const options = await fixture(t, { model }, { TEST_MODEL_KEY: "" });
  await assert.rejects(loadConfig(options), /Model API key is required/);
  options.env.TEST_MODEL_KEY = "fake-model-key";
  options.env.ARENA_SOLVER_TOKEN = "";
  await assert.rejects(loadConfig(options), /Arena attempt token is required/);
});

test("literal secret values and invalid model capabilities are rejected", async (t) => {
  for (const invalid of [
    { model: { ...model, apiKey: "literal-placeholder" } },
    { model: { ...model, contextWindow: -1 } },
    { model: { ...model, maxTokens: 0 } },
    { model: { ...model, reasoning: false, thinkingLevel: "high" } },
  ]) {
    const options = await fixture(t, invalid);
    await assert.rejects(loadConfig(options));
  }
});

test("max thinking is accepted only with an explicit provider mapping", async (t) => {
  const unmapped = await fixture(t, { model: { ...model, reasoning: true, thinkingLevel: "max" } });
  await assert.rejects(loadConfig(unmapped), /thinkingLevelMap\.max/);

  const invalid = await fixture(t, {
    model: {
      ...model,
      reasoning: true,
      thinkingLevel: "max",
      thinkingLevelMap: { max: "" },
    },
  });
  await assert.rejects(loadConfig(invalid), /thinkingLevelMap\.max/);

  const options = await fixture(t, {
    model: {
      ...model,
      reasoning: true,
      thinkingLevel: "max",
      thinkingLevelMap: { max: "max", xhigh: null },
    },
  });
  const loaded = await loadConfig(options);
  assert.equal(loaded.model.thinkingLevel, "max");
  assert.deepEqual(loaded.model.thinkingLevelMap, { max: "max", xhigh: null });
});

test("config JSON is mandatory and global .env or OPENAI variables are not read", async (t) => {
  await assert.rejects(loadConfig(), /--config/);
  const options = await fixture(t, {}, { OPENAI_API_KEY: "irrelevant-fixture-key" });
  await assert.rejects(loadConfig(options), /Model apiKeyEnv/);
});

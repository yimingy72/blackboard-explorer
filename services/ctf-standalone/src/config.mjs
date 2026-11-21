import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

function requiredString(value, name) {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${name} is required`);
  return value.trim();
}

function serviceUrl(value, name) {
  let url;
  try { url = new URL(requiredString(value, name)); }
  catch { throw new TypeError(`${name} must be a valid HTTP(S) URL`); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash || url.search || /[<>]/.test(value)) {
    throw new TypeError(`${name} must be an HTTP(S) URL without credentials or placeholders`);
  }
  return url.href.replace(/\/$/, "");
}

function privateValue(object, name, value) {
  Object.defineProperty(object, name, { value, enumerable: false });
}

const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

function thinkingLevelMap(value) {
  if (value === undefined) return undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Model thinkingLevelMap must be an object");
  }
  const map = {};
  for (const [level, mapped] of Object.entries(value)) {
    if (!THINKING_LEVELS.includes(level)) {
      throw new TypeError(`Model thinkingLevelMap contains unknown level ${level}`);
    }
    if (mapped !== null && (typeof mapped !== "string" || !mapped.trim())) {
      throw new TypeError(`Model thinkingLevelMap.${level} must be a non-empty string or null`);
    }
    map[level] = mapped === null ? null : mapped.trim();
  }
  return map;
}

export function parseArenaHelp(markdown) {
  const text = markdown.replace(/\\_/g, "_");
  const renderedFor = text.trimStart().match(/^# Cyber Arena — Challenges API\s+Rendered for (a connectivity check|a single attempt)\./)?.[1];
  const mode = renderedFor === undefined ? null : renderedFor === "a connectivity check" ? "connectivity_test" : "attempt";
  const urls = [...text.matchAll(/\b(?:ARENA_SOLVER_BASE_URL|BENCHMARK_BASE_URL)\s*=\s*["']?(https?:\/\/[^\s`"']+)/g)]
    .map((match) => serviceUrl(match[1], "Help arena base URL"));
  if (new Set(urls).size > 1) throw new TypeError("Help contains conflicting arena base URLs");
  const attemptId = text.match(/(?:^|[\n\s])Attempt:\s*([^\s`<>]+)/)?.[1] ?? null;
  const expiration = text.match(/\battempt expires at\s*:?\s*([^\s;`<>]+)/i)?.[1] ?? null;
  const normalizedExpiration = expiration?.replace(/^[`"'([{*_]+|[`"')\]}<>*_.,;:]+$/g, "") ?? null;
  const expiresAt = normalizedExpiration === null ? null : Date.parse(normalizedExpiration);
  if (expiration !== null && !Number.isFinite(expiresAt)) throw new TypeError("Help attempt expiration is invalid");
  return { baseUrl: urls[0] ?? null, attemptId, expiresAt, mode };
}

export async function loadConfig({ configPath, env = process.env, helpPath = "/challenge/cyber_arena_help.md" } = {}) {
  if (!configPath) throw new TypeError("Pass --config with the explicit local model configuration file");
  let config;
  try { config = JSON.parse(await readFile(configPath, "utf8")); }
  catch { throw new TypeError("The local configuration file could not be read as JSON"); }
  if (!config || typeof config !== "object" || Array.isArray(config)) throw new TypeError("Configuration must be an object");
  let helpMarkdown = env.CYBER_ARENA_HELP_MARKDOWN;
  if (!helpMarkdown?.trim()) {
    try { helpMarkdown = await readFile(helpPath, "utf8"); }
    catch (error) {
      if (error.code !== "ENOENT") throw new TypeError("The injected arena help file could not be read");
      helpMarkdown = "";
    }
  }
  const parsedHelp = parseArenaHelp(helpMarkdown);
  const explicitArena = config.arena ?? {};
  if (explicitArena.mode !== undefined && !["attempt", "connectivity_test"].includes(explicitArena.mode)) {
    throw new TypeError("Arena mode must be attempt or connectivity_test");
  }
  if (parsedHelp.mode && explicitArena.mode && parsedHelp.mode !== explicitArena.mode) {
    throw new TypeError("Local arena mode conflicts with the injected attempt help");
  }
  const injectedUrls = [
    parsedHelp.baseUrl,
    env.ARENA_SOLVER_BASE_URL && serviceUrl(env.ARENA_SOLVER_BASE_URL, "ARENA_SOLVER_BASE_URL"),
    env.BENCHMARK_BASE_URL && serviceUrl(env.BENCHMARK_BASE_URL, "BENCHMARK_BASE_URL"),
  ].filter(Boolean);
  if (new Set(injectedUrls).size > 1) throw new TypeError("Injected arena base URLs conflict");
  const injectedBaseUrl = injectedUrls[0] ?? null;
  const baseUrl = serviceUrl(explicitArena.baseUrl ?? injectedBaseUrl, "Arena base URL");
  if (injectedBaseUrl && baseUrl !== injectedBaseUrl) throw new TypeError("Local arena URL conflicts with the injected attempt");
  const token = requiredString(env[explicitArena.tokenEnv ?? "ARENA_SOLVER_TOKEN"], "Arena attempt token");
  if (/[\r\n]/.test(token)) throw new TypeError("Arena attempt token contains an invalid header character");
  const modelInput = config.model ?? {};
  if (Object.hasOwn(modelInput, "apiKey") || Object.hasOwn(explicitArena, "token")) {
    throw new TypeError("Credentials must be referenced by environment variable name");
  }
  const apiKeyEnv = requiredString(modelInput.apiKeyEnv, "Model apiKeyEnv");
  const apiKey = requiredString(env[apiKeyEnv], "Model API key");
  const levelMap = thinkingLevelMap(modelInput.thinkingLevelMap);
  const model = {
    provider: requiredString(modelInput.provider, "Model provider"),
    id: requiredString(modelInput.id, "Model id"),
    api: requiredString(modelInput.api, "Model api"),
    baseUrl: serviceUrl(modelInput.baseUrl, "Model base URL"),
    contextWindow: modelInput.contextWindow,
    maxTokens: modelInput.maxTokens,
    reasoning: modelInput.reasoning ?? false,
    thinkingLevel: modelInput.thinkingLevel ?? "off",
    ...(levelMap ? { thinkingLevelMap: levelMap } : {}),
  };
  for (const name of ["contextWindow", "maxTokens"]) {
    if (!Number.isInteger(model[name]) || model[name] <= 0) throw new TypeError(`Model ${name} must be a positive integer`);
  }
  if (typeof model.reasoning !== "boolean" || !THINKING_LEVELS.includes(model.thinkingLevel)) {
    throw new TypeError("Model reasoning settings are invalid");
  }
  if (!model.reasoning && model.thinkingLevel !== "off") throw new TypeError("Thinking requires a reasoning model");
  if (["xhigh", "max"].includes(model.thinkingLevel)
    && typeof model.thinkingLevelMap?.[model.thinkingLevel] !== "string") {
    throw new TypeError(`Model thinking level ${model.thinkingLevel} requires a non-null thinkingLevelMap.${model.thinkingLevel}`);
  }
  privateValue(model, "apiKey", apiKey);
  const attemptId = parsedHelp.attemptId ?? env.ARENA_ATTEMPT_ID ?? null;
  const arena = {
    baseUrl,
    mode: explicitArena.mode ?? parsedHelp.mode ?? "attempt",
    attemptId,
    expiresAt: parsedHelp.expiresAt,
    identity: createHash("sha256").update(baseUrl).update("\0")
      .update(attemptId ? `attempt:${attemptId}` : `token:${token}`).digest("hex"),
    helpMarkdown: helpMarkdown.split(token).join("[redacted]").split(apiKey).join("[redacted]"),
  };
  privateValue(arena, "token", token);
  return { workDir: resolve(config.workDir ?? "/workspace/ctf-run"), model, arena };
}

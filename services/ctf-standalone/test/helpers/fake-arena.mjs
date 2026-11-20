import { createServer } from "node:http";
import { once } from "node:events";

export async function startFakeArena({ challenges = [], token = "fixture-token", maxEnvironments = 3, testing = false, mode = "attempt" } = {}) {
  if (!["attempt", "connectivity_test"].includes(mode)) throw new TypeError("Unknown fake arena mode");
  const states = new Map(challenges.map((definition) => [definition.unique_code, {
    definition, flags: definition.flags ?? [definition.flag], acceptedFlags: new Set(),
    started: false, active: false, completed: false, generation: 0,
  }]));
  if (states.size !== challenges.length || [...states.values()].some((state) => !state.definition.unique_code
    || !Array.isArray(state.flags) || state.flags.length === 0 || new Set(state.flags).size !== state.flags.length
    || state.flags.some((flag) => typeof flag !== "string" || !flag))) {
    throw new TypeError("Fake challenges need unique codes and a flag or distinct flags array");
  }
  const calls = [];
  const stats = { starts: 0, submits: 0, closes: 0, finishes: 0, activeMax: 0 };
  const faults = [];
  let cumulativeScore = 0;
  let finished = false;

  function snapshot(state) {
    const { definition } = state;
    return {
      unique_code: definition.unique_code,
      description: definition.description ?? `Local fixture ${definition.unique_code}`,
      name: definition.name ?? definition.unique_code,
      difficulty: definition.difficulty ?? "easy",
      points: definition.points ?? 100,
      total_score: definition.points ?? 100,
      testing,
      flag_count: state.flags.length,
      correct_flag_count: state.acceptedFlags.size,
      is_completed: state.completed,
      container: definition.container ?? false,
      container_status: definition.container ? (state.active ? "running" : "stopped") : "none",
      generation: state.generation,
    };
  }

  function files(state) {
    const { handout } = state.definition;
    if (!handout) return [];
    return [{ path: handout.path, size: Buffer.byteLength(handout.content) }];
  }

  function writeJson(response, status, payload) {
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(payload));
  }

  function reject(response, status, code, message, detail = null) {
    writeJson(response, status, { code, message, detail });
  }

  function accept(state, flag) {
    const awarded = testing ? 0 : (state.definition.points ?? 100) / state.flags.length;
    state.acceptedFlags.add(flag);
    state.completed = state.acceptedFlags.size === state.flags.length;
    if (state.completed) state.active = false;
    cumulativeScore += awarded;
    return { correct: true, awarded, cumulative_score: cumulativeScore,
      correct_flag_count: state.acceptedFlags.size, total_flag_count: state.flags.length, testing };
  }

  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://fixture");
      if (request.headers.benchmark_token !== token) {
        reject(response, 404, "task_not_found", "Invalid attempt identity");
        return;
      }
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const raw = Buffer.concat(chunks).toString();
      const body = raw ? JSON.parse(raw) : null;
      const operation = url.pathname === "/openapi/v1/challenges" ? "list" : url.pathname.split("/").at(-1);
      const uniqueCode = body?.unique_code ?? url.searchParams.get("unique_code");
      calls.push({ operation, uniqueCode, method: request.method, body });
      const countName = { start: "starts", submit: "submits", close: "closes", finish: "finishes" }[operation];
      if (countName) stats[countName] += 1;
      if (!url.pathname.startsWith("/openapi/v1/challenges") || !["list", "start", "submit", "close", "finish", "files", "help"].includes(operation)) {
        reject(response, 404, "not_found", "Unknown solver endpoint");
        return;
      }
      if (finished) {
        reject(response, 409, "invalid_state", "Attempt is already finished");
        return;
      }
      const fault = faults.find((entry) => entry.operation === operation && entry.times > 0 && (!entry.uniqueCode || entry.uniqueCode === uniqueCode));
      if (fault) fault.times -= 1;
      if (fault?.kind === "http") {
        reject(response, fault.status ?? 503, fault.code ?? "resource_unavailable", fault.message ?? "Injected transient failure", fault.detail ?? null);
        return;
      }
      if (fault?.kind === "disconnect") {
        response.destroy();
        return;
      }
      if (operation === "list") {
        writeJson(response, 200, { challenges: [...states.values()].map(snapshot) });
        return;
      }
      if (operation === "help") {
        response.writeHead(200, { "Content-Type": "text/markdown" });
        response.end(`# Cyber Arena — Challenges API\nRendered for ${mode === "connectivity_test" ? "a connectivity check" : "a single attempt"}. Do not cache or reuse it.\n`);
        return;
      }
      if (operation === "finish") {
        finished = true;
        if (fault?.kind === "finished_disconnect") response.destroy();
        else writeJson(response, 200, { finished: true, cumulative_score: cumulativeScore });
        return;
      }
      const state = states.get(uniqueCode);
      if (!state) {
        reject(response, 404, "challenge_not_found", "Challenge is not part of this attempt");
        return;
      }
      if (mode === "connectivity_test" && ["start", "close"].includes(operation)) {
        reject(response, 409, "invalid_state", "A connectivity probe has no environment; skip start and close");
        return;
      }
      if (operation === "start") {
        const active = [...states.values()].filter((entry) => entry.active);
        if (state.definition.container && !state.active && !state.completed && active.length >= maxEnvironments) {
          reject(response, 409, "environment_limit", "All challenge environment slots are occupied", { challenges: active.map(snapshot) });
          return;
        }
        state.started = true;
        if (state.definition.container && !state.active && !state.completed) {
          state.generation += 1;
          state.active = true;
        }
        stats.activeMax = Math.max(stats.activeMax, [...states.values()].filter((entry) => entry.active).length);
        writeJson(response, 200, { ...snapshot(state), ...(fault?.kind === "pending" ? { container_status: "pending" } : {}) });
        return;
      }
      if (operation === "close") {
        state.active = false;
        state.started = false;
        if (fault?.kind === "closed_disconnect") response.destroy();
        else writeJson(response, 200, { unique_code: uniqueCode, closed: true, container_status: "stopped" });
        return;
      }
      if (operation === "files") {
        const path = url.searchParams.get("path");
        if (path === null) {
          writeJson(response, 200, { files: files(state) });
        } else if (state.definition.handout?.path === path) {
          response.writeHead(200, { "Content-Type": "application/octet-stream" });
          response.end(state.definition.handout.content);
        } else {
          reject(response, 404, "file_not_found", "Handout path does not exist");
        }
        return;
      }
      if (state.acceptedFlags.has(body.flag)) {
        reject(response, 409, "duplicate", "A correct flag was already accepted");
      } else if (!state.started && mode !== "connectivity_test") {
        reject(response, 409, "challenge_not_started", "Start this challenge before submitting");
      } else if (!state.flags.includes(body.flag)) {
        if (mode === "connectivity_test") finished = true;
        writeJson(response, 200, { correct: false, awarded: 0, cumulative_score: 0,
          correct_flag_count: state.acceptedFlags.size, total_flag_count: state.flags.length,
          message: "The submitted flag is incorrect", testing });
      } else {
        const receipt = accept(state, body.flag);
        if (mode === "connectivity_test") finished = true;
        if (fault?.kind === "accepted_disconnect") response.destroy();
        else writeJson(response, 200, receipt);
      }
    } catch {
      if (!response.headersSent) reject(response, 400, "invalid_request", "Invalid local mock request");
      else response.destroy();
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    token,
    mode,
    calls,
    stats,
    setFault(operation, options = {}) {
      faults.push({ operation, kind: "http", times: 1, ...options });
    },
    async close() {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

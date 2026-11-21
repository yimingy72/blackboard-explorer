const CHALLENGES_PATH = "/openapi/v1/challenges";

export class ArenaError extends Error {
  constructor(message, { status = 0, code = "transport_error", detail = null, outcomeUnknown = false } = {}) {
    super(message);
    this.name = "ArenaError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.transient = (status === 0 && code === "transport_error") || status === 429 || status >= 500;
    this.outcomeUnknown = outcomeUnknown;
  }
}

function nonempty(value, label) {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${label} must be a nonempty string`);
  return value;
}

export function challengeCompleted(challenge) {
  return challenge.is_completed === true || (
    Number.isInteger(challenge.flag_count) && challenge.flag_count > 0 &&
    Number.isInteger(challenge.correct_flag_count) && challenge.correct_flag_count >= challenge.flag_count
  );
}

export class ArenaClient {
  #token;
  #fetch;
  #filePaths = new Map();

  constructor({ baseUrl, token, timeoutMs = 15000, downloadTimeoutMs = 10 * 60 * 1000,
    fetchImpl = fetch, cumulativeScore = null }) {
    const url = new URL(baseUrl);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new TypeError("Arena base URL must be an HTTP(S) URL without credentials, query, or fragment");
    }
    if (!["", "/", "/openapi/v1", "/openapi/v1/"].includes(url.pathname)) {
      throw new TypeError("Arena base URL must identify the service root or /openapi/v1");
    }
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new TypeError("Arena timeout must be positive");
    if (!Number.isFinite(downloadTimeoutMs) || downloadTimeoutMs <= 0) {
      throw new TypeError("Arena download timeout must be positive");
    }
    this.baseUrl = url.origin;
    this.timeoutMs = timeoutMs;
    this.downloadTimeoutMs = downloadTimeoutMs;
    this.cumulativeScore = Number.isFinite(cumulativeScore) && cumulativeScore >= 0 ? cumulativeScore : null;
    this.#token = nonempty(token, "Arena token");
    if (/[\r\n]/.test(token)) throw new TypeError("Arena token contains an invalid header character");
    this.#fetch = fetchImpl;
  }

  #redact(value) {
    if (typeof value === "string") return value.split(this.#token).join("[redacted]");
    if (Array.isArray(value)) return value.map((entry) => this.#redact(entry));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, this.#redact(entry)]));
    }
    return value;
  }

  #downloadResponse(response, externalSignal) {
    if (!response.body) return response;
    const reader = response.body.getReader();
    let settled = false;
    let timer;
    let abortListener;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      timer = undefined;
      if (externalSignal && abortListener) externalSignal.removeEventListener("abort", abortListener);
      abortListener = undefined;
    };
    const stream = new ReadableStream({
      start: (controller) => {
        timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          const error = new ArenaError("Arena handout download timed out", {
            code: "download_timeout",
          });
          controller.error(error);
          void reader.cancel(error).catch(() => {});
          cleanup();
        }, this.downloadTimeoutMs);
        timer.unref?.();
        if (!externalSignal) return;
        abortListener = () => {
          if (settled) return;
          settled = true;
          const error = new ArenaError("Arena handout download was cancelled", { code: "cancelled" });
          controller.error(error);
          void reader.cancel(error).catch(() => {});
          cleanup();
        };
        if (externalSignal.aborted) abortListener();
        else externalSignal.addEventListener("abort", abortListener, { once: true });
      },
      pull: async (controller) => {
        if (settled) return;
        try {
          const { done, value } = await reader.read();
          if (settled) return;
          if (done) {
            settled = true;
            controller.close();
            cleanup();
          } else {
            controller.enqueue(value);
          }
        } catch (error) {
          if (settled) return;
          settled = true;
          controller.error(error instanceof ArenaError ? error
            : new ArenaError("Arena handout download failed", { code: "transport_error" }));
          cleanup();
        }
      },
      cancel: async (reason) => {
        settled = true;
        cleanup();
        await reader.cancel(reason).catch(() => {});
      },
    }, { highWaterMark: 0 });
    return new Response(stream, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  }

  async #request(action = "", { method = "GET", uniqueCode, body, path, signal, responseType = "json" } = {}) {
    const url = new URL(CHALLENGES_PATH + (action ? `/${action}` : ""), this.baseUrl);
    if (uniqueCode !== undefined) url.searchParams.set("unique_code", nonempty(uniqueCode, "Challenge code"));
    if (path !== undefined) url.searchParams.set("path", path);
    const timeoutController = new AbortController();
    const timeout = setTimeout(() => timeoutController.abort(), this.timeoutMs);
    timeout.unref?.();
    const headers = { BENCHMARK_TOKEN: this.#token };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      const response = await this.#fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, timeoutController.signal]) : timeoutController.signal,
        redirect: "manual",
      });
      if (!response.ok) {
        const text = await response.text();
        let payload;
        try { payload = JSON.parse(text); } catch { payload = {}; }
        const error = payload?.error && typeof payload.error === "object" ? payload.error : payload;
        throw new ArenaError(this.#redact(typeof error?.message === "string" ? error.message : `Arena HTTP ${response.status}`), {
          status: response.status,
          code: typeof error?.code === "string" ? this.#redact(error.code) : `http_${response.status}`,
          detail: this.#redact(error?.detail ?? null),
          outcomeUnknown: method === "POST" && response.status >= 500,
        });
      }
      if (responseType === "response") return this.#downloadResponse(response, signal);
      if (responseType === "text") return this.#redact(await response.text());
      if (response.status === 204) return null;
      try { return this.#redact(await response.json()); }
      catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        throw new ArenaError("Arena returned invalid JSON", {
          status: response.status, code: "invalid_response", outcomeUnknown: method === "POST",
        });
      }
    } catch (error) {
      if (error instanceof ArenaError) throw error;
      throw new ArenaError(signal?.aborted ? "Arena request was cancelled" : "Arena request failed or timed out", {
        code: signal?.aborted ? "cancelled" : "transport_error",
        outcomeUnknown: method === "POST",
      });
    } finally {
      clearTimeout(timeout);
    }
  }

  async listChallenges(options = {}) {
    const payload = await this.#request("", options);
    const challenges = Array.isArray(payload) ? payload : payload?.challenges;
    if (!Array.isArray(challenges) || challenges.some((item) => !item || typeof item.unique_code !== "string" || !item.unique_code.trim())) {
      throw new ArenaError("Arena challenge list has an unsupported shape", { code: "invalid_response" });
    }
    if (new Set(challenges.map((item) => item.unique_code)).size !== challenges.length) {
      throw new ArenaError("Arena challenge list contains duplicate codes", { code: "invalid_response" });
    }
    return challenges;
  }

  start(uniqueCode, options = {}) {
    return this.#request("start", { ...options, method: "POST", uniqueCode });
  }

  close(uniqueCode, options = {}) {
    return this.#request("close", { ...options, method: "POST", uniqueCode });
  }

  finish(options = {}) {
    return this.#request("finish", { ...options, method: "POST" });
  }

  help(options = {}) {
    return this.#request("help", { ...options, responseType: "text" });
  }

  async listFiles(uniqueCode, options = {}) {
    const payload = await this.#request("files", { ...options, uniqueCode });
    const files = Array.isArray(payload) ? payload : payload?.files;
    if (!Array.isArray(files) || files.some((entry) => !entry || typeof entry.path !== "string" || !entry.path.trim())) {
      throw new ArenaError("Arena handout listing has an unsupported shape", { code: "invalid_response" });
    }
    this.#filePaths.set(uniqueCode, new Set(files.map((file) => file.path)));
    return files;
  }

  readFile(uniqueCode, path, options = {}) {
    if (!this.#filePaths.get(uniqueCode)?.has(path)) {
      throw new ArenaError("Read handouts using a path returned by this challenge's files listing", { code: "unlisted_handout" });
    }
    return this.#request("files", { ...options, uniqueCode, path, responseType: "response" });
  }

  async submit(uniqueCode, flag, options = {}) {
    nonempty(flag, "Flag");
    try {
      const receipt = await this.#request("submit", {
        ...options, method: "POST", body: { unique_code: nonempty(uniqueCode, "Challenge code"), flag },
      });
      if (typeof receipt?.correct !== "boolean") {
        return { outcome: "unknown", receipt, reason: "Submit response omitted a boolean correct field", cumulativeScore: this.cumulativeScore };
      }
      if (receipt.correct && Number.isFinite(receipt.cumulative_score) && receipt.cumulative_score >= 0) {
        this.cumulativeScore = Math.max(this.cumulativeScore ?? 0, receipt.cumulative_score);
      }
      return { outcome: receipt.correct ? "accepted" : "rejected", receipt, cumulativeScore: this.cumulativeScore };
    } catch (error) {
      if (!(error instanceof ArenaError)) throw error;
      if (error.code === "duplicate") return this.reconcileSubmission(uniqueCode, { ...options, duplicate: true });
      if (!error.outcomeUnknown) throw error;
      return {
        outcome: "unknown", receipt: null,
        error: { code: error.code, message: error.message, status: error.status },
        cumulativeScore: this.cumulativeScore,
      };
    }
  }

  async reconcileSubmission(uniqueCode, { duplicate = false, baselineCorrectFlagCount = null, ...options } = {}) {
    try {
      const challenge = (await this.listChallenges(options)).find((item) => item.unique_code === uniqueCode);
      const progressed = challenge
        && Number.isSafeInteger(baselineCorrectFlagCount) && baselineCorrectFlagCount >= 0
        && Number.isSafeInteger(challenge.correct_flag_count)
        && challenge.correct_flag_count > baselineCorrectFlagCount;
      const accepted = Boolean(challenge) && (duplicate || challengeCompleted(challenge) || progressed);
      return {
        outcome: accepted ? "accepted" : "unknown",
        receipt: null,
        challenge: challenge ?? null,
        duplicate,
        reconciled: true,
        ...(accepted && !challengeCompleted(challenge)
          ? { reason: duplicate
            ? "Official progress confirms that the flag was already accepted"
            : "Official progress advanced after the unknown submission" }
          : {}),
        cumulativeScore: this.cumulativeScore,
      };
    } catch (error) {
      if (!(error instanceof ArenaError) || !error.transient) throw error;
      return {
        outcome: "unknown", receipt: null, duplicate, reconciled: false,
        error: { code: error.code, message: error.message, status: error.status },
        cumulativeScore: this.cumulativeScore,
      };
    }
  }
}

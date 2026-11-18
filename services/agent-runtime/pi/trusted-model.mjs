import { createHash } from "node:crypto";
import { isIP } from "node:net";

import { calculateCost } from "@earendil-works/pi-ai/models";
import { Agent, fetch as pinnedFetch } from "undici";

const MAX_BODY_BYTES = 8 * 1024 * 1024;
const count = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 1_000_000_000;
const safeId = (value) => typeof value === "string" && /^[A-Za-z0-9._:\/-]{1,256}$/u.test(value) ? value : undefined;

export function trustedModelTransport(model, scope, claim) {
  const { addresses, max_output_tokens: maximum, allowed_tools: allowed } = scope;
  const endpoint = new URL(`${model.baseUrl.replace(/\/$/, "")}/chat/completions`);
  if (model.api !== "openai-completions" || endpoint.username || endpoint.password ||
      endpoint.search || endpoint.hash || !Array.isArray(addresses) || !addresses.length ||
      addresses.length > 32 || addresses.some((address) => !isIP(address))) {
    throw new Error("The approved provider transport is unavailable");
  }
  const pinned = addresses.map((address) => ({ address, family: isIP(address) }));
  // The trusted Runtime resolves and approves the addresses. TLS still uses the original hostname.
  const dispatcher = new Agent({ connect: { lookup(host, options, callback) {
    if (host !== endpoint.hostname) return callback(new Error("Unexpected provider hostname"));
    const available = pinned.filter((item) => !options.family || item.family === options.family);
    if (!available.length) return callback(new Error("No approved provider address"));
    if (options.all) callback(null, available);
    else callback(null, available[0].address, available[0].family);
  } } });
  let claimed = false;
  return {
    async fetch(url, init) {
      if (claimed || new URL(url).href !== endpoint.href || init?.method !== "POST" ||
          typeof init.body !== "string" || Buffer.byteLength(init.body) > MAX_BODY_BYTES) {
        throw new Error("Unapproved provider dispatch or implicit retry");
      }
      const payload = JSON.parse(init.body);
      const requested = payload?.max_completion_tokens ?? payload?.max_tokens;
      if (payload?.model !== model.id || payload.stream !== true || (payload.n ?? 1) !== 1 ||
          payload.stream_options?.include_usage !== true || !Number.isSafeInteger(requested) ||
          requested < 1 || requested > maximum ||
          (allowed && (payload.tools ?? []).some((tool) => !allowed.includes(tool?.function?.name)))) {
        throw new Error("The provider body is outside the original model approval");
      }
      claimed = true;
      const request_sha256 = createHash("sha256").update(init.body, "utf8").digest("hex");
      const receipt = await claim({ request_sha256 });
      if (receipt?.send_allowed !== true) throw new Error("The original provider dispatch is already closed");
      init.signal?.throwIfAborted();
      return pinnedFetch(endpoint, { ...init, dispatcher, redirect: "error" });
    },
    close: () => dispatcher.destroy(),
  };
}

export function openAiUsageEvidence() {
  let finished = false;
  let finalUsage;
  let invalid = false;
  return {
    observe(chunk) {
      if (chunk?.choices?.some?.((choice) => typeof choice?.finish_reason === "string")) finished = true;
      const usage = chunk?.usage;
      if (!finished || usage === null || usage === undefined) return;
      if (typeof usage !== "object" || Array.isArray(usage)) { invalid = true; return; }
      const reportedHits = [
        usage.prompt_tokens_details?.cached_tokens, usage.prompt_cache_hit_tokens, usage.cached_tokens,
      ].filter((value) => value !== undefined && value !== null);
      const cacheRead = reportedHits[0] ?? 0;
      const cacheMiss = usage.prompt_cache_miss_tokens;
      if (reportedHits.some((value) => !count(value) || value !== cacheRead) ||
          (cacheMiss !== undefined && cacheMiss !== null &&
            (!count(cacheMiss) || cacheRead + cacheMiss !== usage.prompt_tokens))) {
        invalid = true;
        return;
      }
      const cacheWrite = usage.prompt_tokens_details?.cache_write_tokens ?? 0;
      const reasoning = usage.completion_tokens_details?.reasoning_tokens ?? 0;
      const numbers = [usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, cacheRead, cacheWrite, reasoning];
      if (!numbers.every(count) || cacheRead + cacheWrite > usage.prompt_tokens ||
          reasoning > usage.completion_tokens || usage.total_tokens !== usage.prompt_tokens + usage.completion_tokens) {
        invalid = true;
        return;
      }
      const next = { input: usage.prompt_tokens - cacheRead - cacheWrite, output: usage.completion_tokens,
        cacheRead, cacheWrite, reasoning, totalTokens: usage.total_tokens };
      if (finalUsage && JSON.stringify(next) !== JSON.stringify(finalUsage)) invalid = true;
      finalUsage = next;
    },
    result(model, message, status, requestId, pricePresent) {
      const provider_request_id = safeId(requestId) ?? safeId(message.responseId) ?? null;
      if (invalid || !finished || !finalUsage || status < 200 || status >= 300 || !Number.isInteger(status) ||
          !["stop", "length", "toolUse"].includes(message.stopReason) ||
          Object.entries(finalUsage).some(([key, value]) => (message.usage[key] ?? 0) !== value)) {
        return { usage: null, amount_usd: null, provider_request_id };
      }
      const usage = { ...finalUsage, ...(message.usage.cacheWrite1h === undefined ? {} : { cacheWrite1h: message.usage.cacheWrite1h }) };
      if (usage.cacheWrite1h !== undefined && (!count(usage.cacheWrite1h) || usage.cacheWrite1h > usage.cacheWrite)) {
        return { usage: null, amount_usd: null, provider_request_id };
      }
      if (!pricePresent) return { usage, amount_usd: null, provider_request_id };
      const { total } = calculateCost(model, { ...usage, cost: {} });
      if (!Number.isFinite(total) || total < 0) return { usage: null, amount_usd: null, provider_request_id };
      return { usage, amount_usd: total.toPrecision(15), provider_request_id };
    },
  };
}

// PiMessagesEvent drops the growing partial transcript; the pinned Runner SDK reconstructs it.
export function piMessagesDelta(event) {
  if (event.type === "start") return { type: "start" };
  if (event.type === "done" || event.type === "error") return null;
  const { type, contentIndex } = event;
  const base = { type, contentIndex };
  switch (type) {
    case "text_start":
    case "thinking_start":
      return base;
    case "text_delta":
    case "thinking_delta":
    case "toolcall_delta":
      return { ...base, delta: event.delta };
    case "text_end":
      return { ...base, content: event.content, contentSignature: event.partial.content[contentIndex].textSignature };
    case "thinking_end": {
      const thinking = event.partial.content[contentIndex];
      return { ...base, content: event.content, contentSignature: thinking.thinkingSignature, redacted: thinking.redacted };
    }
    case "toolcall_start": {
      const tool = event.partial.content[contentIndex];
      return { ...base, id: tool.id, toolName: tool.name };
    }
    case "toolcall_end":
      return { ...base, toolCall: event.toolCall };
    default:
      throw new Error("Unsupported native Pi message event");
  }
}

import { createHash, randomUUID } from "node:crypto";
import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
import { resolve, join } from "node:path";
import { spawn } from "node:child_process";
import { finished } from "node:stream/promises";
import { createReadTool, createWriteTool, createEditTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { rememberProcess, forgetProcess, recoverProcesses } from "./session-processes.mjs";

const PREVIEW_BYTES = 48 * 1024;

function killGroup(pid) {
  if (!pid) return;
  try { process.kill(process.platform === "win32" ? pid : -pid, "SIGKILL"); }
  catch (error) { if (error.code !== "ESRCH") throw error; }
}

function toolEnvironment(overrides, secrets) {
  const env = { ...process.env, ...overrides };
  for (const name of ["ARENA_SOLVER_TOKEN", "BENCHMARK_TOKEN", "CTF_MODEL_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"]) delete env[name];
  for (const [name, value] of Object.entries(env)) if (secrets.has(value?.trim())) delete env[name];
  return env;
}

export function createLocalTools({ cwd, outputDir = join(cwd, "tool-output"), env = {}, secrets = [] }) {
  const processes = new Set();
  const privateValues = new Set(secrets.filter((value) => typeof value === "string" && value.length));
  const files = [createReadTool(cwd), createWriteTool(cwd), createEditTool(cwd)].map((tool) => ({
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters,
    local: true,
    execute: (args, { callId, signal, onUpdate }) => tool.execute(callId, args, signal, onUpdate),
  }));
  const bash = {
    name: "bash",
    description: "Execute a command in the shared Linux container. Set cwd explicitly when needed. "
      + "Returns exit status and the last 48 KiB of output; complete output is saved to the returned file. "
      + "Optional timeout is in seconds. Child processes are cleaned up when this command finishes or is cancelled.",
    parameters: Type.Object({
      command: Type.String({ minLength: 1 }),
      cwd: Type.Optional(Type.String()),
      timeout: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 2147483 })),
    }),
    local: true,
    async execute(args, { callId = randomUUID(), signal } = {}) {
      signal?.throwIfAborted();
      if (typeof args.command !== "string" || !args.command.trim()) throw new Error("A command is required");
      if (args.timeout !== undefined && (!Number.isFinite(args.timeout) || args.timeout <= 0
        || args.timeout > 2147483)) throw new Error("Invalid command timeout");
      await mkdir(outputDir, { recursive: true, mode: 0o700 });
      const filename = createHash("sha256").update(callId).digest("hex") + ".log";
      const outputPath = join(outputDir, filename);
      const processPath = join(outputDir, filename + ".process.json");
      const output = createWriteStream(outputPath, { flags: "wx", mode: 0o600 });
      let outputError;
      output.on("error", (error) => { outputError = error; });
      const started = performance.now();
      const child = spawn("/bin/bash", ["-s"], {
        cwd: resolve(cwd, args.cwd ?? "."),
        env: toolEnvironment(env, privateValues),
        detached: process.platform !== "win32",
        stdio: ["pipe", "pipe", "pipe"],
      });
      if (child.pid) processes.add(child.pid);
      let preview = Buffer.alloc(0);
      let bytes = 0;
      let timedOut = false;
      let spawnError;
      const stop = () => killGroup(child.pid);
      child.stdin.on("error", () => {});
      child.once("spawn", () => {
        try {
          rememberProcess(processPath, child.pid);
          child.stdin.end(args.command + "\n");
        } catch (error) { spawnError = error; stop(); }
      });
      const timeout = args.timeout === undefined ? undefined : setTimeout(() => {
        timedOut = true;
        stop();
      }, args.timeout * 1000);
      const consume = (stream) => stream.on("data", (chunk) => {
        bytes += chunk.length;
        preview = Buffer.concat([preview, chunk]).subarray(-PREVIEW_BYTES);
        if (!output.write(chunk)) {
          stream.pause();
          output.once("drain", () => stream.resume());
        }
      });
      consume(child.stdout);
      consume(child.stderr);
      output.on("error", stop);
      signal?.addEventListener("abort", stop, { once: true });
      if (signal?.aborted) stop();
      let exitCode;
      let exitSignal;
      try {
        [exitCode, exitSignal] = await new Promise((accept) => {
          child.once("error", (error) => { spawnError = error; accept([null, null]); });
          child.once("exit", (code, sig) => { stop(); accept([code, sig]); });
        });
        await new Promise((accept) => {
          if (child.stdout.destroyed && child.stderr.destroyed) accept();
          else child.once("close", accept);
        });
      } finally {
        clearTimeout(timeout);
        signal?.removeEventListener("abort", stop);
        stop();
        processes.delete(child.pid);
        output.end();
        await finished(output);
        forgetProcess(processPath);
      }
      if (outputError) throw outputError;
      if (spawnError) throw spawnError;
      const details = {
        exit_code: exitCode,
        signal: exitSignal,
        timed_out: timedOut,
        aborted: Boolean(signal?.aborted),
        bytes,
        truncated: bytes > PREVIEW_BYTES,
        output_path: outputPath,
        duration_ms: Math.round(performance.now() - started),
      };
      return {
        content: [{ type: "text", text: `${preview.toString("utf8") || "(no output)"}\n${JSON.stringify(details)}` }],
        details,
        isError: exitCode !== 0 || timedOut || Boolean(signal?.aborted),
      };
    },
  };
  return {
    tools: [bash, ...files],
    activeProcesses: () => processes.size,
    abort: () => { for (const pid of processes) killGroup(pid); },
    recover: () => recoverProcesses(outputDir),
  };
}

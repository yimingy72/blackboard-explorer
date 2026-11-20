import { existsSync, readFileSync, writeFileSync, unlinkSync, readdirSync } from "node:fs";
import { join } from "node:path";

function processInfo(pid) {
  try {
    const raw = readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = raw.slice(raw.lastIndexOf(")") + 2).trim().split(/\s+/);
    return { pid, state: fields[0], group: Number(fields[2]), session: Number(fields[3]), start: fields[19] };
  } catch (error) { if (error.code === "ENOENT" || error.code === "ESRCH") return null; throw error; }
}

const bootId = () => readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();

export function rememberProcess(path, pid) {
  const info = process.platform === "linux" ? processInfo(pid) : null;
  if (process.platform === "linux" && !info) return false;
  if (info && (info.group !== pid || info.session !== pid)) throw new Error("Shell process group identity is inconsistent");
  writeFileSync(path, JSON.stringify({ pid, platform: process.platform, boot_id: info ? bootId() : null,
    start: info?.start, created_at: new Date().toISOString() }) + "\n", { flag: "wx", mode: 0o600 });
  return true;
}

export function forgetProcess(path) {
  try { unlinkSync(path); } catch (error) { if (error.code !== "ENOENT") throw error; }
}

function groupMembers(pid) {
  return readdirSync("/proc").filter((name) => /^\d+$/.test(name)).map((name) => processInfo(Number(name)))
    .filter((info) => info && info.group === pid && info.session === pid && !["Z", "X"].includes(info.state));
}

export async function recoverProcesses(directory) {
  if (!existsSync(directory)) return [];
  const recovered = [];
  for (const name of readdirSync(directory).filter((item) => item.endsWith(".process.json"))) {
    const path = join(directory, name);
    const saved = JSON.parse(readFileSync(path, "utf8"));
    if (!Number.isSafeInteger(saved.pid) || saved.pid <= 1 || saved.platform !== "linux"
      || process.platform !== "linux" || !/^\d+$/.test(saved.start ?? "") || typeof saved.boot_id !== "string") {
      throw new Error(`Cannot verify interrupted shell ownership in ${path}`);
    }
    if (saved.boot_id !== bootId()) {
      forgetProcess(path);
      recovered.push({ pid: saved.pid, outcome: "previous-boot-ended" });
      continue;
    }
    const leader = processInfo(saved.pid);
    if (leader && (leader.start !== saved.start || leader.group !== saved.pid || leader.session !== saved.pid)) {
      forgetProcess(path);
      recovered.push({ pid: saved.pid, outcome: "original-process-ended" });
      continue;
    }
    // A live process group retains its numeric ID even after its leader exits.
    const deadline = Date.now() + 3000;
    while (groupMembers(saved.pid).length) {
      try { process.kill(-saved.pid, "SIGKILL"); }
      catch (error) { if (error.code !== "ESRCH") throw error; }
      if (Date.now() >= deadline) throw new Error(`Interrupted shell group ${saved.pid} has not stopped`);
      await new Promise((accept) => setTimeout(accept, 10));
    }
    forgetProcess(path);
    recovered.push({ pid: saved.pid, outcome: "stopped" });
  }
  return recovered;
}

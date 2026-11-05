import assert from "node:assert/strict";
import { cp, mkdir, readFile, readdir, rm } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(root, "node_modules/pi-subagents-source");
const destination = join(root, ".fixture/pi-subagents");
const lock = JSON.parse(await readFile(join(root, "package-lock.json"), "utf8"));
const revision = "b6647222bd2e849021054292927ef39fea0793ed";
assert.equal(lock.packages["node_modules/pi-subagents-source"].resolved,
  `https://codeload.github.com/nicobailon/pi-subagents/tar.gz/${revision}`);
assert.equal(JSON.parse(await readFile(join(source, "package.json"), "utf8")).version, "0.85.0");
await mkdir(dirname(destination), { recursive: true });
await rm(destination, { recursive: true, force: true });
await mkdir(destination);
for (const path of await readdir(source)) {
  if (["package.json", "src", "index.ts", "agents", "skills", "prompts", "docs", "README.md", "CHANGELOG.md", "LICENSE"].includes(path)
      || path.endsWith(".mjs") || path.endsWith(".cjs")) {
    await cp(join(source, path), join(destination, path), { recursive: true });
  }
}
const patched = spawnSync("patch", ["-p1", "--batch", "-i", join(root, "patches/pi-subagents-0.85.0-session-host.patch")], {
  cwd: destination, encoding: "utf8",
});
if (patched.status !== 0) throw new Error(`The pinned plugin fixture patch did not apply: ${patched.stderr}\n${patched.stdout}`);
process.stdout.write("Prepared isolated pi-subagents 0.85.0 patched fixture (not production)\n");

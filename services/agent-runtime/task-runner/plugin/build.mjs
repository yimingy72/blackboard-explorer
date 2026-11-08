import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const plugin = dirname(fileURLToPath(import.meta.url));
const root = dirname(plugin);
const modules = join(root, "node_modules");
const source = join(modules, "pi-subagents-source");
const stage = join(modules, ".bbx-helper-source");
const output = join(modules, "pi-subagents");
const revision = "b6647222bd2e849021054292927ef39fea0793ed";
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const original = JSON.parse(await readFile(join(source, "package.json"), "utf8"));
const lock = JSON.parse(await readFile(join(root, "package-lock.json"), "utf8"));
assert.equal(original.version, "0.85.0");
assert.equal(lock.packages["node_modules/pi-subagents-source"].resolved,
  `https://codeload.github.com/nicobailon/pi-subagents/tar.gz/${revision}`);
assert.equal(ts.version, "5.9.3");

const patches = [
  ["p1-session-host.patch", "199aec748d9590bce43f20b057224b7ed5a0ade33ca5993661b6879b93cb9f69"],
  ["p4g-hosted-foreground.patch", "f9b280c79554efb941ca8f14c8a22e6ba9ce5d136b8cb5a806b670942aed4afd"],
];
await rm(stage, { recursive: true, force: true });
await rm(output, { recursive: true, force: true });
await mkdir(stage);
await mkdir(output);
try {
  for (const name of await readdir(source)) {
    if (["src", "index.ts", "agents", "skills", "prompts", "README.md", "LICENSE"].includes(name) ||
        /\.(?:mjs|cjs)$/.test(name)) await cp(join(source, name), join(stage, name), { recursive: true });
  }
  for (const [name, expected] of patches) {
    const patch = join(plugin, name);
    assert.equal(sha(await readFile(patch)), expected);
    const applied = spawnSync("patch", ["-p1", "--batch", "--fuzz=0", "-i", patch], {
      cwd: stage, encoding: "utf8",
    });
    if (applied.status !== 0) throw new Error(`Required helper patch did not apply: ${name}`);
  }
  const files = [];
  async function compile(directory) {
    const names = (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name < b.name ? -1 : 1);
    for (const item of names) {
      const input = join(directory, item.name);
      if (item.isDirectory()) { await compile(input); continue; }
      const name = relative(stage, input);
      let bytes = await readFile(input);
      const target = name.endsWith(".ts") && !name.endsWith(".d.ts") ? name.replace(/\.ts$/, ".js") : name;
      if (target !== name) {
        const compiled = ts.transpileModule(bytes.toString("utf8"), {
          fileName: input, reportDiagnostics: true,
          compilerOptions: { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext,
            rewriteRelativeImportExtensions: true },
        });
        assert.ok(!compiled.diagnostics?.some((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error), name);
        bytes = Buffer.from(compiled.outputText);
      }
      await mkdir(dirname(join(output, target)), { recursive: true });
      await writeFile(join(output, target), bytes);
      files.push({ path: target, sha256: sha(bytes) });
    }
  }
  await compile(stage);
  await writeFile(join(output, "package.json"), JSON.stringify({
    name: "pi-subagents", version: "0.85.0-bbx.p4g", private: true, type: "module",
    exports: Object.fromEntries(Object.entries(original.exports).map(([name, path]) => [name, path.replace(/\.ts$/, ".js")])),
  }, null, 2) + "\n");
  await writeFile(join(output, "bbx-build.json"), JSON.stringify({ revision, patches, files }, null, 2) + "\n");
} finally { await rm(stage, { recursive: true, force: true }); }

import "./prepare-plugin-fixture.mjs";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import ts from "typescript";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(root, ".fixture/pi-subagents");
const output = join(root, ".fixture/pi-subagents-patched");
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const patchHash = sha256(await readFile(join(root, "patches/pi-subagents-0.85.0-session-host.patch")));
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
const files = [];
async function compile(directory) {
  for (const item of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
    const input = join(directory, item.name);
    const path = relative(source, input);
    if (item.isDirectory()) { await compile(input); continue; }
    if (path === "package.json") continue;
    let bytes = await readFile(input);
    let target = path;
    if (path.endsWith(".ts") && !path.endsWith(".d.ts")) {
      target = path.slice(0, -3) + ".js";
      const result = ts.transpileModule(bytes.toString("utf8"), {
        fileName: input, reportDiagnostics: true,
        compilerOptions: { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext, rewriteRelativeImportExtensions: true },
      });
      assert.ok(!result.diagnostics?.some((item) => item.category === ts.DiagnosticCategory.Error), `Cannot transpile ${path}`);
      bytes = Buffer.from(result.outputText);
    }
    await mkdir(dirname(join(output, target)), { recursive: true });
    await writeFile(join(output, target), bytes);
    files.push({ path: target, sha256: sha256(bytes) });
  }
}
await compile(source);
const original = JSON.parse(await readFile(join(source, "package.json"), "utf8"));
const candidate = {
  ...original, version: "0.85.0-bbx.p1", private: true, main: "./index.js",
  exports: Object.fromEntries(Object.entries(original.exports).map(([name, path]) => [name, path.replace(/\.ts$/, ".js")])),
  pi: { ...original.pi, extensions: ["./index.js"] },
  files: ["index.js", "src", "*.mjs", "*.cjs", "agents", "skills", "prompts", "docs", "bbx-patch.json"],
};
delete candidate.scripts;
delete candidate.devDependencies;
await writeFile(join(output, "package.json"), JSON.stringify(candidate, null, 2) + "\n");
await writeFile(join(output, "bbx-patch.json"), JSON.stringify({
  baseVersion: "0.85.0", revision: "b6647222bd2e849021054292927ef39fea0793ed", patchSha256: patchHash,
  compiler: `typescript@${ts.version}`, build: "transpile-only local P1 candidate; not an official release", files,
}, null, 2) + "\n");
const packed = spawnSync("npm", ["pack", "--ignore-scripts", "--json", "--pack-destination", dirname(output), "--cache", join(root, ".fixture/npm-cache")], {
  cwd: output, encoding: "utf8",
});
if (packed.status !== 0) throw new Error(`Cannot pack the local candidate: ${packed.stderr}`);
const [artifact] = JSON.parse(packed.stdout);
await writeFile(join(root, ".fixture/patched-artifact.json"), JSON.stringify({
  filename: artifact.filename, integrity: artifact.integrity, shasum: artifact.shasum, patchSha256: patchHash,
}, null, 2) + "\n");
const host = join(root, ".fixture/host");
await mkdir(join(host, "node_modules"), { recursive: true });
await rm(join(host, "node_modules/pi-subagents"), { recursive: true, force: true });
await symlink("../../pi-subagents-patched", join(host, "node_modules/pi-subagents"));
await cp(join(root, "test/daemon/fixtures/parent-policy.mjs"), join(host, "parent-policy.mjs"));
process.stdout.write(`Built local patched candidate: ${artifact.filename} (${artifact.integrity})\n`);

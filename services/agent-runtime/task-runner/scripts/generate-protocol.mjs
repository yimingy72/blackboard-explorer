import { spawnSync } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const result = spawnSync("uv", ["run", "--no-sync", "python", "-m", "bbx_contracts.runner"], {
  cwd: fileURLToPath(new URL("../../../../", import.meta.url)), encoding: "utf8",
  env: { ...process.env, UV_CACHE_DIR: "/private/tmp/bbx-uv-cache" },
});
if (result.status !== 0) throw new Error("Cannot generate the Runner protocol from bbx_contracts.runner");
JSON.parse(result.stdout);
await writeFile(new URL("../protocol.schema.json", import.meta.url), result.stdout);

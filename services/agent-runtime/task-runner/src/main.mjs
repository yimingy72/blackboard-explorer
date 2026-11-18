import { readFile } from "node:fs/promises";
import { validate } from "./protocol.mjs";
import { HttpPlatform, MountedCredentials } from "./platform.mjs";
import { TaskRunner } from "./runner.mjs";

try {
  const config = validate("RunnerBootstrap", JSON.parse(await readFile(process.argv[2], "utf8")));
  const credentials = new MountedCredentials(config.credential_file, config.fence.task_id);
  const runner = new TaskRunner(config, new HttpPlatform(config, credentials), credentials);
  process.once("SIGTERM", () => { void runner.stop(); });
  process.once("SIGINT", () => { void runner.stop(); });
  await runner.run();
} catch {
  process.stderr.write("Task Runner stopped at its control or persistence boundary\n");
  process.exitCode = 1;
}

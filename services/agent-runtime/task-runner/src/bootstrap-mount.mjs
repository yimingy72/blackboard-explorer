import { setTimeout } from "node:timers/promises";

export async function readBootstrapMount(
  read, signal, protectedBoard, { now = Date.now, pause = setTimeout } = {},
) {
  // Protected Blackboard startup projects the host, edge, and then Runner Secrets.
  const deadline = now() + (protectedBoard ? 360_000 : 180_000);
  while (true) {
    signal.throwIfAborted();
    try {
      return await read();
    } catch (error) {
      if (error.code !== "ENOENT" || now() >= deadline) throw error;
      await pause(200, undefined, { signal });
    }
  }
}

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";

import { HttpPlatform, MountedCredentials } from "../src/platform.mjs";
import { RunnerSession } from "../src/session.mjs";
import { fixture } from "./support.mjs";

test("direct native Pi runs tools and SDK summaries through the private Runtime", {
  skip: !process.env.BBX_TEST_PI_RUNTIME,
  timeout: 30000,
}, async (t) => {
  const taskId = randomUUID();
  const h = await fixture(t, ["one"], { taskId });
  h.state.directModelUrl = process.env.BBX_TEST_PI_RUNTIME;
  const credentials = new MountedCredentials(h.config.credential_file, taskId);
  const platform = new HttpPlatform(h.config, credentials);
  const spec = h.spec("one");
  spec.model.model_revision = 2;
  const payload = { kind: "prepare", spec };
  const command = { ...h.command(payload), payload };
  const lease = await platform.acquire(command);
  const slot = new RunnerSession(h.config, spec, command.payload_digest, lease, platform, credentials);
  try {
    await slot.initialize();
    slot.session.settingsManager.applyOverrides({
      compaction: { enabled: false, reserveTokens: 512, keepRecentTokens: 32 },
    });
    await slot.session.prompt("The first isolated request. ".repeat(350));
    assert.equal(h.state.toolCalls.length, 1);
    assert.equal(h.state.modelCalls.filter((call) => call.action === "turn").length, 2);
    const anchor = slot.session.sessionManager.getLeafId();
    await slot.session.prompt("More isolated fixture data. ".repeat(350));
    const branch = await slot.session.navigateTree(anchor, { summarize: true });
    assert.equal(branch.cancelled, false);
    await slot.session.prompt("A third isolated request. ".repeat(350));
    const compacted = await slot.session.compact();
    assert.ok(compacted.summary);
    assert.ok(h.state.modelCalls.filter((call) => call.action === "summary").length >= 2);
    const ids = h.state.modelCalls.map((call) => call.logicalCallId);
    assert.equal(new Set(ids).size, ids.length);
    assert.equal(h.state.attempts.size, 0);
    h.assertClean();
  } finally {
    await slot.close();
  }
});

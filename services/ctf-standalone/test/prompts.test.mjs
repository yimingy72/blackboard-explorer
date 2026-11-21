import assert from "node:assert/strict";
import test from "node:test";
import { rolePrompt } from "../src/prompts.mjs";

test("Lead prompt makes offline preparation and online slot handling explicit", () => {
  const prompt = rolePrompt({
    role: "lead",
    agentId: "lead",
    cwd: "/workspace/ctf-run/work/lead",
    toolSummary: "TOOL-MANIFEST",
  });
  assert.match(prompt, /container=false/);
  assert.match(prompt, /arena_prepare_offline/);
  assert.match(prompt, /离线优先是 Lead 的比赛调度策略/);
  assert.match(prompt, /平台不替 Lead 硬门禁离线\/在线启动顺序/);
  assert.match(prompt, /在线环境同时最多 3 个是赛事 API 配额/);
  assert.match(prompt, /API 返回 409、environment_limit 或其他配额拒绝时保留原始错误/);
  assert.match(prompt, /close_environment=false 不算释放名额/);
  assert.match(prompt, /arena_start 不会自动下载在线题附件/);
  assert.match(prompt, /必须用 arena_files 查询文件清单并逐个下载/);
  assert.match(prompt, /priority 的 0 最高、3 最低/);
  assert.match(prompt, /为离线题设置更小的 priority/);
  assert.match(prompt, /调度器不会额外硬编码离线题优先/);
  assert.match(prompt, /API 返回 409、environment_limit 或其他配额拒绝时保留原始错误/);
  assert.match(prompt, /只有决定是否增员或怀疑 CPU、内存、磁盘、事件循环存在压力时才读取 resources/);
  assert.match(prompt, /初始团队是 20 个同级 Worker/);
  assert.match(prompt, /你拥有 grow_workers 权限和主动增员的义务/);
  assert.match(prompt, /待援助但无空闲 Worker/);
  assert.match(prompt, /资源仍有安全余量且存在有效并行工作时，应优先调用 grow_workers 扩员/);
  assert.match(prompt, /困难题先只分配一名主 Worker 并进入 assembling\/需要援助/);
  assert.match(prompt, /主 Worker 不应空等第二名成员/);
  assert.match(prompt, /平台不会为援助绕过新题/);
  assert.match(prompt, /只有没有 new_challenges，且存在空闲 Worker 或你通过 grow_workers 增员后，才加入援助 Worker/);
  assert.match(prompt, /累计解题达到 30 分钟且没有援助时，平台会开放援助/);
  assert.match(prompt, /新未认领任务始终高于援助任务/);
  assert.match(prompt, /request_id 由平台自动生成/);
  assert.match(prompt, /下一次写入前使用最新版本/);
  for (const action of [
    "update_challenge: challenge_id,expected_revision",
    "post_fact: challenge_id,expected_revision,statement,evidence",
    "request_assistance: challenge_id,expected_revision,reason",
    "reject_review: challenge_id,expected_revision,review_revision,reason",
    "arena_start: challenge_id",
    "arena_reconcile: {}",
    "arena_prepare_offline: {}",
    "arena_files: challenge_id;[path] optional",
    "submit_flag: challenge_id,candidate_id,review_revision,expected_revision",
    "reconcile_submissions: {}",
    "defer_challenge: challenge_id,reason,expected_revision;[close_environment] optional",
    "resume_challenge: challenge_id,expected_revision",
    "resources: {}",
    "grow_workers: target_total,reason",
    "finish_attempt: reason",
  ]) assert.ok(prompt.includes(action), action);
  assert.match(prompt, /TOOL-MANIFEST/);
});

test("Worker prompt defines selection, evidence, tools and review without subagents", () => {
  const prompt = rolePrompt({
    role: "worker",
    agentId: "worker-7",
    cwd: "/workspace/ctf-run/work/worker-7",
    toolSummary: "TOOL-MANIFEST",
  });
  assert.match(prompt, /首次工作由平台直接分配/);
  assert.match(prompt, /任何 new_challenges 都先于 assistance/);
  assert.match(prompt, /available 非空时，本轮必须实际认领一份工作/);
  assert.match(prompt, /不能空等、拒绝认领或把选择留到以后/);
  assert.match(prompt, /竞争失败就读取最新 available 再选/);
  assert.match(prompt, /priority 相同时再综合预期得分、剩余时间、当前上下文和工具惯性选择/);
  assert.match(prompt, /Board 只统一按 priority 排序/);
  assert.match(prompt, /不要在客户端另造离线硬门禁/);
  assert.match(prompt, /困难题被平台放入 assembling 时不要空等援助/);
  assert.match(prompt, /平台先分配 1 个主 Worker，只有没有新题且存在空闲 Worker 或 Lead 增员时，才加入援助 Worker/);
  assert.match(prompt, /正常写 Fact、Intent、认领方向或提交有证据的候选/);
  assert.match(prompt, /每个 bash 调用结束时会终止该调用启动的整个进程组/);
  assert.match(prompt, /bash 启动的后台进程会随该次调用结束而终止/);
  assert.match(prompt, /paths\.worker_cwd/);
  assert.match(prompt, /任务通知中的 paths 覆盖会话默认 cwd/);
  assert.match(prompt, /只并行相互独立的只读操作/);
  assert.match(prompt, /下一次写入前使用最新版本/);
  assert.match(prompt, /board_act claim/);
  assert.match(prompt, /snapshot_file 记录，path 必须用绝对路径/);
  assert.match(prompt, /request_deferral/);
  for (const action of [
    "claim: challenge_id,expected_revision",
    "post_fact: challenge_id,expected_revision,statement,evidence",
    "post_intent: challenge_id,expected_revision,statement,based_on,expected,method",
    "claim_intent: challenge_id,expected_revision,intent_id,expected_version",
    "release_intent: challenge_id,expected_revision,intent_id,note",
    "request_assistance: challenge_id,expected_revision,reason",
    "request_deferral: challenge_id,expected_revision,reason",
    "submit_review: challenge_id,expected_revision,flag,summary",
  ]) assert.ok(prompt.includes(action), action);
  assert.match(prompt, /parallel=true requires parallel_key/);
  assert.match(prompt, /不要创建子 Agent 或 helper/);
  assert.match(prompt, /TOOL-MANIFEST/);
});

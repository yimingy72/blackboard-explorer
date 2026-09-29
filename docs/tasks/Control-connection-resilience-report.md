# 控制链内部连接断开容错报告

## 本次失败证据

任务 `4891d8c7-7720-45de-8aec-5562809c57a3` 的第 2 轮在 2026-09-29 14:57:07 续跑，15:19:03 的事件 `26111` 将任务标记为 failed，原因为“调度循环异常退出”。运行镜像已包含此前 Responses 顺序修复，源码哈希一致。本轮记录 710 次模型用量事件、0 条 model_error，新增 29 条 Fact、23 条 Intent；13 个 Explore 正常结束，9 个在调度退出后以 runtime_restart 被取消，1 个因会话持久化失败结束。`runtime_restart` 是此清理路径使用的取消原因，实际 runtime 与黑板容器的重启次数均为 0，也没有 OOM 记录。

runtime 日志明确记录调度 future 的 `RemoteProtocolError`。旧代码对每任务 tick/清扫器的内部 HTTP 传输异常没有恢复边界，会进入 finally 并取消全部 Agent，随后 supervisor 标记任务失败。服务端日志未保留可确定连接关闭来源的证据，因此不能进一步断言是 keep-alive、网络或某一具体接口导致。

Agent 28 的 Session PUT 在 15:18:58 返回 200，15:19:01 的下一次 PUT 返回 409。离线 fake 复现了服务端提交后客户端被取消、revision 尚未更新、最终保存冲突的竞态；不能以“会话持久化失败”反推此前所有会话已丢失。

## 完成情况

1. **任务调度 tick**：`SchedulerLoop.tick()` 只捕获内部控制接口（黑板/执行环境控制）的 `httpx.TransportError`。读状态或执行动作过程中断连时，以固定的 `read_state` / `apply_actions` 阶段和异常类写安全日志，并保留调度循环及已有 Agent。下一次事件唤醒或周期 tick 会重新读取黑板状态后决策；本次失败的黑板或 envd 写请求不会原样重发。其它异常仍向上传播。
2. **超时清扫**：`Sweeper.run()` 对一次 `check_once()` 的 `httpx.TransportError` 记录固定 `sweep` 阶段和异常类，下个清扫周期重新读取状态。已提交但响应丢失的 `finish_agent` 不在当前调用内重试；下一轮若已终态便不重复完成。其它异常仍向上传播。
3. **Session 取消与提交**：在原 CAS 锁内完成已经发出的单次 PUT 及必要的已提交快照读回核对，更新 revision 和消息投递状态后，再原样传播首次取消异常。保留 runtime_restart / heartbeat 的取消原因；真实写入失败及外部 revision 冲突仍抛出，不盲目覆盖他人数据，不重发不确定写入。
4. **归档超限保护**：归档接口返回永久 413 时，保留执行容器与所有文件，停止本进程中的自动重复压缩，不登记虚假归档或 cleanup_ready；旧归档不会持续占用新任务槽位。新的 provisioning 或成功删除会清除该阻断标记。其他归档错误保持重试；此保护不扩容、不排除或删除工作区文件。
5. **回归测试**：覆盖控制读写断连后重读恢复、写入已提交后不原样重放、逻辑错误仍传播、Session 提交后的首次/多次取消及丢应答、外部版本冲突仍拒绝、归档 413 保留数据并释放新任务槽位。日志不包含异常原文或凭据。

## 如何验证与实际结果

在仓库根目录执行：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run ruff format --check services/agent-runtime/src/bbx_runtime/scheduler/loop.py services/agent-runtime/src/bbx_runtime/scheduler/sweeper.py services/agent-runtime/tests/test_scheduler_loop.py services/agent-runtime/tests/test_scheduler_sweeper.py
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run ruff check services/agent-runtime/src/bbx_runtime/scheduler/loop.py services/agent-runtime/src/bbx_runtime/scheduler/sweeper.py services/agent-runtime/tests/test_scheduler_loop.py services/agent-runtime/tests/test_scheduler_sweeper.py
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pyright services/agent-runtime/src/bbx_runtime/scheduler/loop.py services/agent-runtime/src/bbx_runtime/scheduler/sweeper.py services/agent-runtime/tests/test_scheduler_loop.py services/agent-runtime/tests/test_scheduler_sweeper.py
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pytest -q services/agent-runtime/tests/test_scheduler_loop.py services/agent-runtime/tests/test_scheduler_sweeper.py services/agent-runtime/tests/test_supervisor.py
```

子代理分别完成控制链 25 项、Session 18 项、supervisor 15 项定向测试，ruff 与 pyright 通过。主代理独立运行 `make check`，695 项普通测试全部通过，ruff format/lint 与 pyright 通过；日志 `/private/tmp/bbx-control-resilience-check.log`。本轮不调用真实模型，也不重放任务目标操作。

主代理重建四个测试镜像并完整运行 `make test-integration`：65 项全部通过，699 项非集成检查排除，耗时 256.92 秒；日志 `/private/tmp/bbx-control-resilience-integration.log`。

## 偏差与待决

- 本补丁仅处理 `httpx.TransportError`，不吞 Blackboard 返回的 `RemoteError`（HTTP 状态错误）及代码逻辑异常；这保留了真正冲突和业务错误的既有失败语义。
- 连接持续中断会延迟调度与超时清扫，恢复后继续；根本断连来源仍需 Blackboard 日志核对。
- 归档容量问题尚未解决：按现有排除规则只读统计，agents/ 有 105,738 个条目、12,778,127,684 字节文件内容，tar 体积下界约 12.86 GB；同时超过现有 8 GiB 展开大小和 100,000 条目上限。只去除依赖缓存仍超限，不能自动删除 work/scan 等可能包含成果的目录。压缩后是否超过 2 GiB 上限尚未知。本轮保留容器与全部原文件，不伪造成功归档；该旧任务仍须完成容量处理或分卷归档后才能续跑。
- 归档容量阻断标记仅在进程内，重启 runtime 会再次尝试一次；它避免无效重复压缩，不替代可持久化的归档错误展示和用户重试入口。

## 下一步与建议提交划分

建议主代理独立审核并完成全套检查后按以下划分提交：

1. `Keep scheduler alive across blackboard transport drops`：`services/agent-runtime/src/bbx_runtime/scheduler/{loop,sweeper}.py` 与 `services/agent-runtime/tests/test_scheduler_{loop,sweeper}.py`。
2. `Preserve checkpoint commits across cancellation`：`session.py` 与 `test_session_checkpoint.py`。
3. `Retain workspaces when archive capacity is exceeded`：`scheduler/supervisor.py` 与 `test_supervisor.py`。
4. `Document control connection recovery`：`docs/tasks/Control-connection-resilience-report.md`。

提交正文按 `AGENTS.md` 补 `Implemented by Codex (gpt-6-sol) for task Control-connection-resilience.`。本轮未提交、合并或部署。

## 本地部署复核（2026-09-29）

主代理已按上述四项划分提交并快进合并到 main（`a283a4b`、`29ed0c8`、`2f45f9e`、`e2ddca8`），随后用最终测试镜像 `90f60d61070a` 更新本地 agent-runtime。部署前无运行、收尾或排队 provisioning 任务；部署后容器 running、重启次数 0、运行锁 1 个、工作台 `/login` 返回 200。Session 与三个调度模块的容器内 SHA-256 与 main 逐项一致。

没有自动重跑原任务或删除原执行容器；完整归档容量仍待处理，旧任务未标记 cleanup_ready，不能据本轮修复宣称已恢复续跑能力。

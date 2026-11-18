# Runner poll 通知复用报告（2026-11-06）

## 1. 完成内容

按《平台架构重构开发方案》11.1 的最小接缝授权实施：

- Runner poll 先尝试订阅现有 Task 通知，再在原短事务内读取权威命令表、核验 owner/binding/业务映射和续租；等待期间不占用数据库连接。保留原 prepare/input 顺序和命令选择条件。
- 空读后的每秒休眠改为通知等待，漏通知最长 5 秒回读；请求截止、最早许可提前续期点、owner 到期和 hard deadline 可以进一步缩短等待。许可被 owner 上限截断且不能再续时，直接按到期点回读，避免空转。
- 新命令、owner 注册/更新和首次 permit 获取在同一事务内调用现有 `Repository.notify`。PG 只在提交后发出通知；回滚不会唤醒。payload 固定为 0，仅表示唤醒，不作为事件版本、命令序号、命令内容或 ACK。
- `SSEDispatcher.subscribe(wait_ready=False)` 为 Runner 提供可选路径，注册和退出清理各限时 0.1 秒；获取不到 listener 就走定时回读。退出同步移除本 poll 的 queue。LISTEN/UNLISTEN 执行中异常或取消可能使 asyncpg 本地记录与数据库状态不一致，因此立即终止该专用通知连接，由原 `_run` 重连并唤醒原订阅者回补；仅等锁超时不终止健康连接，其残留监听由下一次成功注册操作回收。普通 SSE 默认仍等待 ready 后回补，未改消息版本语义。
- API 直接传入应用现有 dispatcher，无新增依赖、配置或通知总线。

## 2. 验证

仅执行受影响范围的检查，未调用真实模型或靶场。使用现有 `ctf_url` testcontainers fixture，随机宿主机端口，容器使用既有 `bbx-ctf-db-*` 任务前缀并自动清理。

```sh
.venv/bin/pytest -q services/blackboard/tests/test_sse.py services/blackboard/tests/test_runner_poll_integration.py
.venv/bin/ruff format --check services/blackboard/src/bbx_blackboard/runner_control.py services/blackboard/src/bbx_blackboard/sse.py services/blackboard/src/bbx_blackboard/runner_control_api.py services/blackboard/tests/test_sse.py services/blackboard/tests/test_runner_poll_integration.py
.venv/bin/ruff check services/blackboard/src/bbx_blackboard/runner_control.py services/blackboard/src/bbx_blackboard/sse.py services/blackboard/src/bbx_blackboard/runner_control_api.py services/blackboard/tests/test_sse.py services/blackboard/tests/test_runner_poll_integration.py
.venv/bin/pyright services/blackboard/src/bbx_blackboard/runner_control.py services/blackboard/src/bbx_blackboard/sse.py services/blackboard/src/bbx_blackboard/runner_control_api.py services/blackboard/tests/test_sse.py services/blackboard/tests/test_runner_poll_integration.py
```

独审修复后的必要复验仅执行：

```sh
.venv/bin/pytest -q services/blackboard/tests/test_sse.py
.venv/bin/ruff format --check services/blackboard/src/bbx_blackboard/sse.py services/blackboard/tests/test_sse.py
.venv/bin/ruff check services/blackboard/src/bbx_blackboard/sse.py services/blackboard/tests/test_sse.py
.venv/bin/pyright services/blackboard/src/bbx_blackboard/sse.py services/blackboard/tests/test_sse.py
```

必要场景覆盖：订阅与首次读取之间提交、prepare/input 顺序、事务提交唤醒与回滚静默、空闲 1 秒后没有重复查表、等待不持连接、取消清理、listener 未 ready 的短许可续期及 owner/hard deadline、无通知停止的有界回读、注册/清理网络或锁阻塞的回退，以及原 SSE 回补/重连/隔离/读取竞态回归。故障 fake 分开记录 callback 与数据库 LISTEN，模拟 LISTEN 已成功但 callback 未登记、callback 已删除但 UNLISTEN 尚未完成的两个窗口；验证不确定连接被终止、正常 SSE 经原重连流程回补，单纯锁超时保留健康连接并能回收残留监听。

实际结果：首次 13 个测试通过（10.97 秒），5 个受影响 Python 文件的 ruff format/check 通过、pyright 为 0 errors / 0 warnings；检查时未发现遗留的 `bbx-ctf-db-*` 容器。独审修复后 9 个 SSE 测试通过，修复涉及的 2 个 Python 文件静态检查通过；未重复执行未改动的 4 个 PG 主体场景。`git diff --check` 通过。

## 3. 偏差与待决

- 无设计偏差。现有 `RunnerControl.poll` 的直接调用可不传 dispatcher，按同样的有界定时策略回读。
- 注册或清理失败不会留下本 poll 的 queue。监听 I/O 状态不确定时通过专用连接终止清除数据库监听；仅等锁超时的已知监听可能保留到下一次成功 listener 操作或重连，它不承载命令数据，也不阻塞 Runner 续租回读。
- 保留原自动派工和 P3c 工作区改动；原有 `cancel_admitted` 挂钩不属于本任务，未修改其业务行为。原样基线、精确增量 patch、clean HEAD 候选和 SHA-256 清单留在 ignored 的 `.data/runner-notify-20261106/`。
- 未提交、推送或合并；按本次授权未单独运行根目录全量门禁，由组合候选统一执行。

## 4. 下一步与建议提交

交独立审核后仅修必要问题，再与 P0 候选组合并执行统一门禁。建议单独提交本次 3 个生产文件、`test_sse.py`、新增 PG 测试及本报告，英文信息：`perf(blackboard): wake runner polls with task notifications`。

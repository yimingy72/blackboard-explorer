# P3b Runner 控制与 journal 实施报告

2026-11-06 · GPT-6.1 Sol / max。依赖 P3a 8461cb4；必要定点、独立审核与干净候选门禁全部组成目标已通过，未暂存/提交，不代表 V1 已可执行或 G1–G7 全部联通。

## 1. 实现内容

- 0023 仅新增四表。owner 保留 `(task_id, epoch)` 历史且每 Task 唯一 current，Task 锁内接管，独立于 legacy ctf_control.epoch。绑定保存冻结 spec/精确 prepare 原文、真实业务成员/Assignment/turn 映射与同 SDK journal；原 owner/命令/回执证据跨接管保留。
- service 管理注册/续期/撤销、绑定与有限命令；独立 Runner audience 只可访问 runner-control。原 Principal 不增加权限，普通用户/Agent/Runner 自报 supported contract 均不能登记 readiness 或解锁旧 V1 start/claim。
- 初次 hard_deadline 按可信 Task 剩余时间冻结，跨接管不延长；owner 租约 60 秒，短许可 30 秒。credential_expires_at 精确对应 JWT exp，并与 owner expires_at、会话许可、固定 hard_deadline 分开。注册/受信续期返回 no-store Token，Runner 无凭据签发或 owner 续期能力。
- 命令与回执按原 owner epoch 隔离，保存精确 UTF-8 payload_json/HTTP body 摘要；poll 重启重放原 prepare，健康输入不因尚未 acquire 被越过。等待不持数据库连接，实际续期立即返回；历史 closed 状态与不可投递命令由 SQL 过滤并限量。
- journal 以 previous cursor 做 CAS，首次确认才登记 Pi SDK ID。原 ctf_messages claim、recipient、purpose、turn、generation、token 同事务核验；仅 native input proof 推进 applied，迟到 accepted 不降级，更新的已确认 cursor 可作语义幂等回执。
- instruction 同事务追加真实 turn.assignment_message_ids，保留 checkpoint_revision，ctf_messages.session_revision 为 null；delivery 元数据按原 sender+recipient 寻址。service.events 对 V1 已寻址 delivery 的 SQL 与最终投影保持一致，legacy 与未寻址隔离保留。
- 单 binding 失效撤许可并持久 close，健康会话继续；原 input queued/accepted 状态保持待核。close/abort 仅本地证据，不释放 Worker，不恢复 Task，不把 unknown 认作成功。迟到 pending journal 限原已登记范围，只存旧证据；不推进当前 input/cursor/许可。
- prompt 仅对受信发布文本条件读，冻结的模型/工具/环境不因文字修改扩权。Task 归档与删除包含四表，公开 DTO/普通 Task 状态不携带 Token 或原会话历史。

## 2. 验证记录

必要命令：

```sh
.venv/bin/pytest -q -m integration services/blackboard/tests/test_runner_control_integration.py
```

三组 PostgreSQL 综合场景 **3 passed / 13.10 秒**。其中第二组通过真实 `task-runner/src/main.mjs` 子进程、真实随机端口 Blackboard HTTP 与隔离 PostgreSQL 测试库，MCP/模型仅复用 P3a 假 producer：prepare 回执已提交而 HTTP 回包被测试闩锁阻断，杀本次拥有的 Node 进程后，同 PVC/SDK 重启；真实输入经原 journal 确认变为 applied；service 接管 owner 后恢复同一 binding/SDK，真实 close 回执落库。未把手工 DTO 验证称为生产全链路。

轮询查询进一步只取活跃 binding 必要元数据（不拉固定 prompt/prepare），命令按可投递条件与 100 条上限筛选；锁外等待为 1 秒检查间隔。该必要收敛后只重跑受影响的真实 Node 场景，**1 passed / 16.96 秒**；包括受控 clock 验证实际续期立即返回、等待时连接池无占用。V1 delivery sender/未寻址隔离与 legacy events/SSE 定点 **4 passed / 23 deselected / 1.78 秒**。聚焦 pyright **0 errors / 0 warnings**。

首轮新 harness 为 **3 failed / 22.15 秒**，原文请求缺 application/json、管理读路径拼错、长轮询 teardown 未闭合；第二轮为 **3 failed / 21.91 秒**，删除前置时序不符既有 guard，及测试子进程 stdin 未闭合。修正测试接线/收尾与前置顺序后通过，未削弱业务断言、未修改旧 automatic 失败。短证据在 `.data/p3b-control-20261106/targeted-checks.log`。

干净候选基于 **8461cb4**，独立 frozen Python 环境的五个 bbx 包导入均位于候选路径；共享 uv 缓存缺少构建依赖 metadata，定向刷新未恢复，改用任务私有缓存后按锁安装 94 包成功，未重建源环境。Pi 旧版本依赖按其锁安装，前端只复用已安装第三方依赖。API/合同 JSON/TS 分别从原树及干净候选真实处理器生成；候选 `tsc --noEmit` **exit 0**。

根门禁首次 `make check` 已完成 **477 文件格式检查**，随后本批 instruction 兼容写库循环有一处未使用解包变量，lint B007 导致 **exit 2**，其他阶段尚未运行。只把局部变量 `message` 改为 `_message`，无业务变化；随后继续 `make lint typecheck pi-check test`，**exit 0**，其中 **477 文件格式、lint 全过、pyright 0 errors/0 warnings、Pi 5 passed、pytest 2615 passed / 453 deselected / 206.37 秒**。未重复 PostgreSQL 或已过测试。首次 `make-check.log`/`make-check-exit.txt` 与续跑 `make-check-resume.log`/`make-check-resume-exit.txt` 均保留，不将首次失败写成 exit 0。

## 3. 精确修改与依赖

共享文件修改前原样保存在 `.data/p3b-control-20261106/baseline/` 与 `baseline-manifest.json`。现批包括新 runner_control 合同/独立鉴权/聚焦处理器/router/0023、三个 PG 综合场景及本地进程 helper；schema、API 注册与固定边界、合同导出、归档/删除及 V1 delivery 元数据为必要窄增量。P3a runner.py/Node 源码保持原提交；测试只导入其已冻结 support 并清理本次运行材料。

候选只应用本批 14 个源码/测试/迁移文件、6 个新合同 JSON、OpenAPI/TS 两个生成物及两份任务文档，共 **24 文件**。API import 三行因源树旧 WIP 上下文不匹配，仅在候选等价锚点加入已审导入；原 `.rej` 与应用日志保留。没有复制旧 WIP、G1 独立改动或 P3a 的其他未提交材料；`clean-candidate.patch` 和双侧文件摘要用于后续精确暂存。

## 4. 偏差与后续

1. 本批只接持久控制/输入/journal/prompt，不提供模型预算代理、工具准入、MCP 业务宿主、K8s/PVC/Secret 创建或迁移。service 管理 API 仍须由后续可信 Runtime/资源控制器供给真实注册与续期；本批不自行放开执行。
2. binding 注册必须已有真实可核的业务 turn/Assignment；缺映射直接拒绝，不为演示填假生产状态。测试以隔离 fixture 提供受信业务前置。
3. PostgreSQL 保存原始控制/回执和 journal 元数据，不保存第二份 Python history，不宣称元数据本身证明远端命令已停止或账本已核清。
4. 原树旧 automatic 五个行为失败继续保留；未读取 `.env`，未调用真实模型/靶场，未推送、合并或部署。

## 5. 提交划分

建议单独提交 `feat: persist fenced Runner control and journal receipts`，仅含上述 24 文件；先由主代理核对最终补丁与暂存范围，再提交。报告不记录自身提交号，提交结果由最终消息给出。

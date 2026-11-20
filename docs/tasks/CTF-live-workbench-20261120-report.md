# CTF 黑板实时交互与启动性能修复报告

## 1. 完成内容

用户要求停止任务 `0fca9f1f-d959-4c75-9ee1-d6cfafe1eb5f`，修复领取任务、Worker 归属、状态与进展、Agent 会话、实时费用，并尽量优化速度。

1. **停止原任务。** 原任务状态为 `stopped`，清理状态为 `complete`，对应执行容器已退出。两个本地验收任务也已结束并清理。
2. **任务领取与分配。** 题目详情提供“自动选择空闲 Worker”或指定 Worker 的分配操作，复用原生派工、任务锁和请求幂等机制。指定忙碌 Worker 时保留队列；题目携带外部标识或连接地址也可正常进入队列。未显式提供材料状态的 ready 请求按 ready 处理，兼容旧的 `no_external_materials`。
3. **归属与状态。** Worker 节点及会话显示正在处理的题目，并支持打开题目详情；接入原生领取、释放和停止事件。派工及改题后的状态写入题目快照。任务结束后，未完成题目显示“已中断”，会话移除“正在处理的任务”导航。
4. **共享进展。** 原生 Fact / Intent 通过状态快照与实时事件进入现有进展展示，带真实事件时间；题目卡片、计数和详情同步更新。
5. **Agent 会话。** Runner 在已有 journal 确认请求中上传本次原始增量，校验 hash、offset、seq 与 anchor 后持久保存。用户打开 Lead / Worker 会话可以读取模型返回的思考、工具参数、执行命令与结果；命令非零退出码和工具异常显示为失败。停止任务后仍可读取本轮新增日志。
6. **费用。** 修复 CTF 请求 `/pi-fees` 时被错误返回 `409 mode_mismatch` 的问题；运行中每 2 秒刷新，终态及费用相关事件主动触发刷新。
7. **启动提速。** Secret 发布后更新原 Pod 注解，触发 kubelet 立即刷新投影挂载，消除两次接近一分钟的等待。原先记录的两段等待约 76 秒和 68 秒。启动阶段前端每秒读取状态，Lead 显示“正在启动执行环境”。
8. **减少无效工作。** Worker 没有空闲席位时不继续逐题查询；会话通过 revision 返回未变化响应；Runner 上传时复用已读取的增量字节。新 Assignment 准备阶段以原有 solver UID/GID 创建独立 Agent 工作目录，消除 Worker 首条命令因目录不存在而失败的情况。

没有新增功能开关或用户操作限制。主站仍提供 Agent 配置、模型管理、MCP 管理和工作空间；实际检查模型与 MCP 的 `can_manage` 均为 `true`。

## 2. 验证与结果

### 自动检查

在本修复工作树执行：

```bash
make check
pnpm --dir web check
npm --prefix services/agent-runtime/task-runner test
.venv/bin/pytest -q -m integration \
  services/blackboard/tests/test_native_worker_dispatch_integration.py \
  services/blackboard/tests/test_runner_control_integration.py \
  -k 'native_worker_claim_evidence_and_original_drain or native_journal_content_is_readable_and_revision_cached'
.venv/bin/pytest -q services/agent-runtime/tests/test_sandbox_agent_workspace.py
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  pnpm --dir web e2e ctf-live-native.spec.ts ctf-workbench.spec.ts \
  --grep-invert '停止成员消息'
git diff --check
```

- `make check`：ruff 格式与 lint、pyright、2316 项普通测试通过；未调用真实模型。
- 前端检查：ESLint、TypeScript、164 项测试通过。
- Runner 检查：24 项通过，2 项原有可选集成测试未启用。
- PostgreSQL 定向集成：4 项通过，覆盖派工、外部题目、指定 Worker、幂等、会话原文持久化与缓存。
- 独立工作目录：3 项通过，覆盖 Lead、Worker、helper 目录及 UID/GID。
- 旧会话与权限相关定向回归：78 项通过。
- 部署前端产物上的关键浏览器用例：6 项通过；新增原生工作台用例验证分配、思考、命令、费用、共享事实及终态显示。
- 一个既有浏览器用例“停止成员消息只排队，恢复显式操作，移除历史只读”仍期待“恢复后送达”，与主线原有的停止成员复盘问答行为不一致。本次定向浏览器组排除了该用例，没有将全量浏览器测试标记为通过。

### 本地真实端到端

入口：`http://127.0.0.1:18089`，完整 `service/blackboard`。

使用现有 `baizhi-deepseek-flash` revision 3，验收范围是本地打印、计算 2+2、登记共享事实。验收任务各设置 USD 1 / 5 分钟预算；这些是测试任务参数，没有改变平台默认设置。

| 任务 | 创建到首笔模型请求 | 结束状态 | 模型调用 | 费用 USD |
| --- | ---: | --- | ---: | ---: |
| 原任务 `0fca9f1f…` | 175.675 秒 | stopped | 38 | 0.869434 |
| 第一轮 `8e1db2f2…` | 35.489 秒 | finished | 15 | 0.074316 |
| 第二轮 `73291d1b…` | 44.875 秒 | stopped | 17 | 0.102332 |

两轮启动时间相对原记录减少约 **74%–80%**。该指标只衡量创建到首笔模型请求，不能等同于完整解题耗时；仍包含本机 Kubernetes 调度、容器与 Runner 准备时间。

第二轮 Worker 第一条命令在 `/workspace/agents/member-1` 成功打印 `BBX_WORKER_OK`，退出码为 0。共享事实 `F1` 显示在题目卡片和关键进展中。Lead 36 条、Worker 23 条原生日志在任务停止后仍可读取。浏览器实际验证了思考展开、工具参数与结果展开、Worker 归属、费用、共享进展、终态“已中断”和当前任务导航清除。

本地证据保存在工作树 `.data/ctf-live-workbench-20261120/`，该目录不提交：

- `make-check.log`、`focused-integration.log`
- `final-startup-timing.json`、`final-execution-check.json`、`final-state.json`
- `browser-live.json`、`browser-retained.json`
- `lead-live.png`、`worker-live.png`、`worker-retained.png`、`challenge-retained.png`
- `verify_live.py`、`verify-browser.cjs`、`verify-retained.cjs`

## 3. 本地部署

- 使用专用分支 `fix/ctf-live-workbench-20261120`，基于 `2d7cf1b`；保留其他工作树的未提交修改。
- 旧完整生产镜像上定向应用补丁，前端保留生产已有的初始输入展示等差异，避免部署主线时回退已有功能。
- `blackboard`、`runtime`、Runner、Sandbox host 使用本任务镜像；具体不可变引用见同目录 `images.json`。
- 最终 Blackboard 引用：`docker.io/library/bbx-live-fix-blackboard@sha256:d83f82daa7511306b97cd0f72896edd9dfe171fa081043f3ae664f822e31f3a9`。
- CTF publication revision 17，board revision 11，runtime / host 引用、acceptance hash 与配置摘要已核对。
- Runtime Pod 权限增加 `patch`，仅用于原 Pod 挂载刷新；请求携带原 Pod UID。
- PostgreSQL、MinIO、conversation-collector 保持原实例；旧受限 management Deployment 为 0 replicas，入口指向完整主站。
- 原任务与两个验收任务 cleanup 均为 `complete`；没有残留的验收任务 Pod。

## 4. 偏差与待决

1. **原任务历史原生会话无法补回。** 旧实现没有持久化这些会话原文，旧任务工作区归档中也没有完整日志。原任务的任务记录、费用与命令审计仍保留，但不能恢复完整思考与工具对话。此次修复从新的 journal 增量开始保存。
2. **原生知识展示兼容。** Fact / Intent 投影为现有前端进展记录结构，未新增数据库表或修改设计正本。后续若需要在历史分页中统一检索原生知识，应再同步设计与接口。
3. **一次命令通道瞬时失败。** 第二轮的后续算术命令曾返回 `The original command was not sent.`，Host 同时记录连接提前结束；Worker 随后成功执行并登记结果。独立目录问题已排除，未把这一次现象宣称为已经解决的传输层问题。
4. **本地部署保留现有生产差异。** 镜像是在已有完整功能版本上应用本次补丁，并非直接以当前主线完整重建；需要在后续发布时归并这些既有差异。
5. 未修改 `docs/design/`、本地 `.env` 或全局配置。

## 5. 共享文件与提交

共享契约修改仅包含：

- `BoardDispatchChallengeRequest`：增加 ready 状态与可选 Worker。
- `BoardChallengeUpdateRequest`：兼容 ready 状态。
- `CtfChallenge`：增加派工状态与请求 Worker。
- `JournalConfirmation`：增加可选原生日志增量及完整性验证。
- 对应 JSON Schema、Runner protocol、OpenAPI 和前端类型随代码同步。

建议并采用一个原子提交，包含契约、Runner、后端、前端、必要测试、RBAC 和本报告：

```text
Fix native CTF live workbench and startup delays

Implemented by Codex (gpt-6-sol) for task CTF-live-workbench-20261120.
```

## 6. 下一步建议

使用新任务验证实际题目的协作流程；继续按创建、容器就绪、Runner 就绪、模型首请求四段观察启动耗时。若再次出现命令连接提前结束，结合保留的执行审计和 Host 记录定位传输链路，避免将该问题误判为工作目录或模型空转。

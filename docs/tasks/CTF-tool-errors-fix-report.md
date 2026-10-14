# CTF 工具错误、生命周期与性能修复报告

审查：2026-10-14；本地发布：2026-10-15。后端/运行时：`/Users/yym/bbx-wt/ctf-cooperation`；前端：`/Users/yym/bbx-wt/ui-rebuild/web`。

## 1. 原报告复核

原报告指出工具经常只返回 `Error: Function failed.`，这一现象属实，但当前源码能给出更具体的根因。

### 1.1 带引用的交付在观测阶段失败

当前 MAF 将函数参数用 Pydantic 校验后，以 Python 模式 `model_dump(exclude_unset=True)` 转为映射。嵌套 `solution.steps[*].record_ids/artifact_ids` 此时含 `UUID` 对象。原 `ToolObservationMiddleware` 只复制映射，随后 HTTPX 编码观测请求时产生 `TypeError`，真正的黑板工具尚未被调用。

离线原生 MAF 复现矩阵：带引用但无观测中间件成功；有观测但不带引用成功；同时带引用和原观测中间件失败。修复后，`record_candidate` 和 `update_challenge` 均可在原生工具循环、JSON HTTP 传输及 checkpoint 中提交同一份完整引用。

修复只用公共 Pydantic `TypeAdapter` 将观测副本转为 JSON 值，保留实际工具参数的类型。不放宽 UUID、引用归属或交付约束。

这解释了报告“去掉引用即成功”的模式。历史会话只保留 MAF 的固定 `FunctionInvocationError` 标记，不能据此声称历史上的每一条失败都由这个原因产生。

### 1.2 需要更正的结论

- 当前工具 schema 已包含 `kind`、`result` 的枚举及引用数量限制；报告所称“枚举未暴露”不成立。
- MAF 的 `max_consecutive_errors=3` 按有错误的工具批次计数。一批可执行多个工具；成功的同批兄弟调用不会使该批变为无错误。达到限制后，后续模型请求不再提供工具，不能理解为第三个独立工具立即中止执行。本次没有调高或关闭这一保护。
- 邻近 `agent.trace.recorded`、`tool_call.recorded` 时间戳可来自同一事务，不能证明工具服务仅耗时 0.1 秒；低 CPU 也不能排除 I/O 或锁等待。
- 原生 MAF 支持一轮多个独立工具并发调用，并非每个工具必然独占一轮模型请求。Lead 总时长除以请求数包含工具、持久化及调度时间，不能作为纯模型延迟。

## 2. 已实施的错误处理修复

1. **参数反馈**：通过 MAF 公共 `input_model` 做同源 Pydantic 校验。只返回至多 8 项 schema 字段路径和错误类型，不返回原始输入、验证消息、上下文或堆栈。结果由既有 checkpoint 保存。
2. **业务拒绝**：补齐重开、负责人/协作者权限、不可用成员、附件未登记、跨题附件/记录等已知错误码。反馈引导读取当前黑板并修正操作。未知 409/422、认证、预算、代次及阶段错误不进入允许列表。
3. **控制故障**：观测、工具结果保存、未知工具异常通过 MAF 公共 `MiddlewareFailure` 边界退出工具循环，runner 精确还原应用封装的原异常，保留已有结算与恢复分类。避免模型把不确定结果当作可随意重试的工具错误。
4. **执行副作用**：执行器在已经可能派发的调用及输出保存范围内使用同一边界，阻止原生循环因普通异常而再次执行相同命令。取消继续按取消传播，既有停止和远端 drain 证明保留。
5. **平台工具状态**：区分 `prepared`、明确未派发的 `rejected`、`dispatched`、`received` 和 `unknown`。明确 preflight 拒绝在保存 `rejected` 后可以重新授权；未知 begin、派发应答或保存结果仍不得盲重放。
6. **平台反馈及恢复**：authorize/begin 的已知 CAS 拒绝可反馈并修正；`expected_revision >= 1` 与后端合同一致。确认未派发的 `rejected` 不再永久阻碍已完成旧回合输入退役，其他未知状态仍保护上下文。
7. **取消身份**：工具已被取消时，错误观测中的第二次取消不替换首次取消；普通错误处理期间的新取消仍优先传播。
8. **前端状态**：用 MAF `Content.additional_properties.ctf_tool_error` 显式标记业务拒绝，历史、checkpoint 及观测中保留标记。前端按该标记显示失败，不猜测任意命令输出中的 JSON `error` 字段。
9. **耗时观测**：工具结果增加单调时钟 `elapsed_ms`。范围包含参数观测、工具执行及工具结果 checkpoint，不包含最后一笔结果观测请求，不称作纯工具服务或纯模型耗时。

## 3. 报告之外的生命周期缺陷

| 缺陷 | 合法触发流程与影响 | 修复 |
| --- | --- | --- |
| 移除负责人后 Lead 不会醒来 | Alice 持有未完成题但空闲，Bob 继续运行；移除 Alice 后题无负责人，仅有 SSE，Lead 邮箱为空，题一直没人接管 | 同事务写入一条幂等 `challenge_available` 邮箱通知；不自动派工，Lead 决策 |
| 重开题仍归已移除成员 | 交付题保留历史归属；移除成员后重开，原实现直接改 `in_progress`，却不能再给该成员派工 | 重开时按既有可用性规则检查负责人；不可用则退回待分配并保留 `previous_owner_id`，通知 Lead |
| 取消题被重新变成未完成 | 取消题后移除旧负责人，原实现将 `cancelled` 改 `blocked`，进而阻碍完成声明 | `completed`、`cancelled` 均保留终态历史；明确重开时才变更工作状态 |

随机 PostgreSQL 回归验证通知与题目更新原子性、另一队员运行时 Lead 也 ready、重复移除不重复发消息、停止/移除/正在停止/旧成员缺失的重开行为，以及取消题不再阻碍完成声明。

## 4. 性能改进与证据

| 路径 | 原开销 | 新行为与实际验证 |
| --- | --- | --- |
| 正常模型响应的费用 outbox 清空 | 已确认计费后，单为清空 outbox 再保存完整 Session | 合入下一笔必须保存的工具、模型或最终 checkpoint；原生“两次响应 + 一次工具 + 最终保存”用例完整保存 **6 → 4 次** |
| 已保存工具结果分页读取 | 取回整个 Session，再在 Python 查 call_id | PostgreSQL JSONPath 绑定 call_id，仅投影全部匹配结果和该 ID 的备用结果；约 **6.6 MB** 合成历史的投影 **小于 1 KB** |
| 费用对账 | 即便 outbox 为空也取回整个 Session | 仅投影两个 usage outbox 与 `ctf_steps`；约 **6.6 MB** 合成历史的投影 **小于 1 KB** |

恢复、启动及异常结算仍立即保存 outbox 清理，正常响应才延后。首次费用凭据持久化、请求 ID 幂等、丢 ACK、重启重复结算、部分失败保留凭据、回合/代次/步数校验均保留。

工具结果查询保留同 call_id 的所有匹配项，重复一致结果可读，冲突仍 409，缺失仍 404；没有用 `LIMIT 1` 隐藏冲突。查询数量未减少：结果路径仍 4 个 SELECT，空费用路径仍 1 个 SELECT。JSONPath 仍需数据库检查该 JSONB 文档；本次主要减少数据库返回、网络传输及 Python 解码的无关历史，不声称消除了所有服务端扫描。

这些是合成路径和原生离线调用的验证，不是实际模型的解题耗时、缓存率或通关率提升数据。未增加真实模型调用。

## 5. 检查与本地发布

实际最终结果：

- `make check`：Ruff 格式与 lint 全绿，Pyright 0 errors/0 warnings；**1878 passed**。24 项警告来自已有 MCP sampling 弃用提示。
- `make test-integration`（按下述命令复用已有镜像）：**325 passed**，387.82 秒，含新生命周期、JSONB 投影、费用凭据恢复及既有跨服务回归。
- 当前前端 `pnpm check`：ESLint、TypeScript 全绿，**108 passed**；`pnpm build` 成功。既有的大 bundle 提示仍存在，本轮未改前端依赖和拆包策略。
- 候选 Linux runtime 的 MAF **1.19.0** 与新增模块导入检查成功；两份候选镜像及实际运行容器均逐文件核对：runtime **71**、blackboard/前端 **157** 文件，无差异。
- 本地应用已恢复运行，[登录地址](http://127.0.0.1:58000/login) HTTP 200，页面入口资源与验证后的前端构建哈希一致。

本地发布仅替换停止后的应用源文件与前端构建，再启动原应用容器；没有重建数据库或 MinIO，没有操作存档桶、卷、凭据或已有配置。运行容器保留原镜像身份，源码字节已与新候选一致；同时更新 `bbx-agent-runtime:latest`、`bbx-ctf-blackboard:latest` 供后续重建使用。

本次候选镜像：

| 镜像 | ID |
| --- | --- |
| `bbx-ctf-tool-errors-runtime:20261014` | `sha256:456f2543710987e3d7decd96132794fa0d1ce2448f2b20fd47fb0ad68fbdd489` |
| `bbx-ctf-tool-errors-blackboard:20261014` | `sha256:72c35659e2b79bc4c8c22cf115a7300a5a50e10e0b5d6b737f1b90cde649ece8` |

应用停止前确认 **0 active tasks / 0 running turns**，关闭黑板后再次核对，避免新任务进入窗口。发布前后 **13 张业务表的数量及内容摘要完全一致**，包括 13 个任务、17 轮运行、80 个 Session、82 个成员、349 回合、651 条团队消息、198 个题目、1050 条记录、26 份 profile、3 份平台配置；schema 仍 **0016**。

证据保存在 `.data/ctf-tool-errors-check.log`、`.data/ctf-tool-errors-integration.log`、`.data/ctf-tool-errors-release/` 的源码清单、前后数据库摘要、镜像/运行源码比对、HTTP 比对和发布验证 JSON。旧应用源文件备份在该目录 `before-source/`，不含 `.env`。

可复现命令：

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env -o image-agent-runtime test-integration
cd /Users/yym/bbx-wt/ui-rebuild/web
pnpm check
pnpm build
```

集成检查复用本机已验证的依赖镜像；随机端口测试容器由夹具清理，不创建真实任务或发起模型请求。

## 6. 偏差与待决

- 保留原报告及历史会话，不重写原始测试事实、不自动解除历史未知执行保护。前端新增显式错误标记对新结果生效，历史未标记的普通 JSON 文本不强行猜测失败。
- MAF 1.19.0 的本地历史 sentinel 也可能触发失败后的 `tool_choice="none"` 清理请求。未找到公共关闭选项；保留逐调用持久化与 SDK 原生清理，验证不执行工具，但可能产生一次经过既有预算门禁的额外模型请求。
- 取消不能撤销已发生的并发兄弟调用副作用，仍依靠现有 stop/drain 屏障；没有借异常封装承诺远端“恰好一次”。
- 不以提高并发数或盲重试代替错误修复；没有修改工作提示词、模型、费用单价、任务预算或 `docs/design/`，没有引入依赖或新缓存服务。
- 未做真实模型/靶机回归；下一轮用户测试才能比较整体解题速度和缓存命中率。
- 本次共享目录只新增 `packages/contracts/src/bbx_contracts/tool_errors.py` 的错误码及脱敏反馈辅助函数；未改 `pyproject.toml`、`uv.lock`、`Makefile` 或 Compose。此前任务已有的合同/生成文件改动仍原样保留。

## 7. 建议提交划分

当前未执行 Git 写操作。工作树还含此前前端、交付与通讯优化的在办改动，提交时应按实际 hunk 区分，不能把这些历史改动算作本报告的新工作。

1. `fix(ctf): preserve typed tool feedback and stop uncertain executions`：合同错误码/字段反馈、观测、Content 标记、公共失败边界、执行/平台/runner 与原生回归；前端工具状态同步。
2. `fix(ctf): restore dispatch after owner removal and challenge reopen`：成员退出通知、终态保护及重开负责人检查、生命周期回归。
3. `perf(ctf): project session reads and coalesce usage cleanup checkpoints`：结果/费用窄投影、正常费用清理 checkpoint 合并及恢复/数据库回归。
4. `docs(ctf): record tool failure audit and performance evidence`：任务与补充报告。

提交正文应按 `AGENTS.md` 附加任务 footer；本轮不合并或推送。

# P2b 公告与多 Flag 实施报告

2026-11-06 · GPT-6.1 Sol / max · 承接知识切片 401d7cf。本文记录实际实现与有限验证，不代表真实平台、V1 Runner 或 G1–G7 已全部联通。

## 1. 完成内容

| 编号 | 实现结果 |
| --- | --- |
| B1 原子业务版本 | 受信 create_task 的内部参数在同一创建事务冻结 challenge-board-v1 与角色 manifest；普通创建 body 不接收内部合同，不存在已有 Task 事后切换入口，旧 Task 缺标记仍为 legacy |
| B2 迁移窗口 | TaskView 安全投影合同；legacy supervisor 在恢复、排队、执行环境创建前跳过 V1。服务端旧 start/claim/recover/fail_start/review 也拒绝 V1，不接受 caller 字符串自称支持；真实绑定/readiness/lease 后续由 P3 接同一门禁 |
| B3 新业务入口 | 新 knowledge/tools 与 REST 共用处理器，角色来自可信 Principal/成员表；只有当前 fenced Lead 可建题。V1 旧 tools/业务写及 Agent 的旧全局状态入口不可绕过；旧 challenge/record 事件也按当前题目范围过滤，越界和缺归属 linked 保留空游标，明确公告事件可全局读取；legacy 权限和 automatic WIP 保留 |
| B4 公告 | Task 级不可变版本、Lead-only 写、request_id 幂等、公告 revision CAS、历史/增量读取。environment_revision 只取平台状态引用，公告文字/请求不能设置 network/readiness |
| B5 受保护候选 | 候选原值以既有密钥材料的域隔离加密保存，公共 DTO/事件/Lead 消息没有明文或密文值；明确的值详情在后端鉴权，允许 Task 用户、Lead、仍获准作者，服务身份不能借通用详情读取 |
| B6 稳定轮次/去重 | verification_round 只由服务端 Task.run_number、题目 scope_revision 和已登记平台目标引用派生，不取可改说明文字；模型不能填写。原值 UTF-8 精确摘要、不 trim，同题同轮同值稳定去重；候选、事件、稳定 Lead 消息同事务 |
| B7 单次提交 | submission_id/operation_id 服务端稳定生成，一个候选只有一个业务提交；不同 request_id 不新发。平台 call_id 保持原 Text 格式；只授予一次发送许可，重复/unknown 不再给值或许可。同题累计回执未决时串行等待下一提交，避免分数乱序 |
| B8 可信捕获 | 派发同时复用既有 ctf.platform.dispatched ledger，绑定原 call_id/source/请求摘要与业务 op/candidate。可信捕获仅核调用绑定和大小，服务端选择 URI/hash 并原样保存不可变 capture；HTML、截断/坏 JSON、空响应也留证，不提前做 Flag 语义验收；调用方不能选任意 Task artifact 盖章 |
| B9 可信回执 | 专属 adapter hook 只从 submission→已登记 call→不可变 capture 取回包；非 HTTP/MCP 签发工具。校验原始对象 hash 后解析规范结果，语义失败写 unknown 并拒签，原始 capture 保留；合法结果签封全部绑定，落库再次核签名、原始/封装对象 hash 与 capture/operation/candidate/source/请求摘要。无捕获拒签，伪 accepted JSON 保持 unknown，未支持来源返回 unsupported |
| B10 多 Flag | accepted 部分正确继续；可信累计分数取最新值、不做 +=；duplicate 不自增，总数未知不自动通关。只有当前目标版本的可信完整回执能把未取消题目改 completed；模型不能传 accepted/score |
| B11 停止/迟到 | Task 停止事件取消 prepared 未派发操作，Task 锁裁定停止/派发竞争；已发真实回执允许归原提交，即使 Task 已 stopped。不会恢复 Task、释放成员/席位或覆盖 cancelled；迟到不发 Lead 消息 |
| B12 保全 | 公共投影重放保留不可变私有值，归档保存新三表与原始回执引用，Task 删除清三表；原始 upstream_response_uri 加入既有归档引用集合 |

## 2. 实际边界

统一业务前缀为 `/api/tasks/{task_id}/ctf/knowledge`。新增 Task 公告/题目/工具清单与工具调用；题内 `/flags` 提供候选、提交与受保护值详情；内部 dispatch/unknown/capture/receipt 只接受服务身份，均不出现在模型工具 manifest 中。明文值详情和内部首次派发响应设置 no-store。

**V1 尚不开放 legacy 模型执行。** 本批冻结的是可审核业务合同，不是凭 manifest 声明就自动启动。P3 必须登记真实 Runner 绑定、支持能力、资源就绪与租约后再开放同一门禁；当前没有部署切换、后台管理 LLM 或第二调度器。

flag_receipt_v1 是本批支持的可信捕获/规范结果协议。假 transport 在测试中通过真实 capture 接口写入原始回包，再调用同一后端 hook；真实 CTF 平台 producer、网络/VPN 和外部 adapter 联通尚未验证。没有把测试签封当成真实平台调用证明。

operation_id 是业务身份，不替代 P3 的控制 request_id、MCP invocation_id 或远端 execution_id。P3 后续关联同一记录，不复制一套远程执行权威。

## 3. 文件与 WIP 范围

- 公共合同：challenge_board.py、models.py 事件类型；ctf.py 仅追加结果 adapter 与 capture 事件类型。
- 新聚焦处理器：board_events.py、task_board.py、flag_board.py；现 knowledge handler/router 复用身份、内容规则和范围。
- 数据：schema.py 追加 announcements/candidates/submissions 三表，迁移 **0022**；repository.py 追加投影/重放和停止取消。
- 窄入口增量：API/ctf_api、CtfService 创建/旧准入、ctf_review 旧认领、legacy supervisor 前置 guard、TaskView 安全投影。
- 生命周期：conversations 归档/删除三表；execenv/archive.py 仅增加 upstream_response_uri 引用键。
- 生成物：按本批合同模型及依赖选择 JSON，OpenAPI/TS 分别由原树和干净候选真实处理器生成，无 UI 改造。
- 测试：新四组 PostgreSQL 综合场景与单个 legacy supervisor guard 回归，未改原五个 automatic 失败断言。

修改前原样基线和 SHA-256 位于本地 `.data/p2b-board-20261106/baseline-manifest.json`。保留原有 WIP，不整文件暂存共享文件。干净候选由 401d7cf 加本批自有变更构成；两个旧 WIP 上下文差异（API 的 dispatch_mode 行、supervisor 的旧 options import）仅在候选中定位本批新增行，没有导入旧 automatic 实现。已核对本批 50 文件与新 HEAD f9ffea8 的 P1 Kubernetes 增量无重叠，自有补丁可应用当前 HEAD。

## 4. 验证结果

```sh
.venv/bin/pytest -q -m integration services/blackboard/tests/test_board_flags_integration.py services/blackboard/tests/test_challenge_knowledge_integration.py
.venv/bin/pytest -q services/agent-runtime/tests/test_versioned_board_guard.py services/agent-runtime/tests/test_ctf_supervisor.py
```

已运行的必要证据：

- 原树四组新 PostgreSQL 综合场景通过；加入不可变 capture/同 Task A/B 串包负例后 **4 passed，5.79 秒**。
- 干净候选隔离 frozen venv，5 个 bbx 包导入路径均位于候选内；本批与知识兼容共 **9 passed，12.93 秒**。同题串行派发补充后本批四组最终 **4 passed，5.98 秒**。
- 干净候选相关 API、鉴权、归档、事件及 supervisor 回归 **108 passed，6.58 秒**；此前原树相关组合 **92 passed，5.48 秒**，未运行无关全量/真实模型。
- 独审指出的 V1 建题事件越界和异常原始回包丢证两项已修，原审核人定点复核通过：受影响的现有两个 PG 综合场景 **2 passed，6.90 秒**；空响应随后并入同一异常回包循环，该场景最终 **1 passed，5.42 秒**。legacy 事件/SSE 回归 **3 passed、22 deselected，1.20 秒**；涉及五个 Python 文件格式/lint 通过，四个行为变更文件 pyright 为 0 errors/0 warnings；生成物刷新后 OpenAPI **1 passed，1.84 秒**。没有增加参数矩阵。
- 现有 legacy supervisor 与新 guard 组合 **20 passed，1.74 秒**。服务端 V1 不接旧 start/claim，普通 body/能力字符串不能解锁，场景中没有创建真实模型请求。
- 候选 21 个本批 Python 文件格式/lint 通过；聚焦 pyright 发现 API 可选 engine/member 两处类型收窄，修正后对应 API 为 0 errors/0 warnings，其余聚焦文件无错误。没有通过放宽检查或改变旧断言处理。
- API、合同 JSON、TS 使用现有导出器与 openapi-typescript 7.13.0 生成；干净 TypeScript 检查通过，候选 OpenAPI 快照检查 **1 passed，1.92 秒**。
- 定点独审通过后执行唯一提交门禁：首次 `make check` 已完成 **467 文件格式检查、lint 全过、pyright 0 errors/0 warnings**，随后因候选缺少 Pi Node 依赖而 **exit 2**，pytest 尚未开始。按锁执行 `npm ci --ignore-scripts --no-audit --no-fund` 安装 89 包，**exit 0**；仅续跑未完成的 `make pi-check test`，**exit 0**，其中 **Pi 5 passed，pytest 2614 passed / 450 deselected / 193.03 秒**。全部组成目标通过，没有重复已过 lint/type、PostgreSQL 或 TypeScript。首次失败日志及退出码保留在 `.data/p2b-board-20261106/make-check.log`、`make-check-exit.txt`，续跑为 `make-check-resume.log`、`make-check-resume-exit.txt`，不将首次环境失败改写为 exit 0。

测试包含：公告权限/幂等/CAS与真实网络状态不变；Worker 通用 events 读不到别题题干/连接和缺归属 linked，自有题目/公告仍可读且每个版本游标保留；受保护值授权、字节精确去重与消息回滚；同候选多 request_id 单次派发、unknown 不重试、伪造回包不验收、A/B 捕获交换拒绝、异常/空原始回包保留且拒签；部分/多 Flag/duplicate 累计分数；停止先获锁取消未派发、stopped 后真实回执不唤醒/不改 cancelled/不动成员；重放、归档、删除保全。

## 5. 偏差与后续

1. 私有密文值是不可变记录，公开事件仅引用其 ID/摘要，不从公开事件反推私有值。重放保留该记录，归档同时保全；若私有记录实际损坏则明确失败，不伪造空值。
2. 不做旧 Task 合同切换。迁移存量任务须先停止 legacy 准入并对账，不属于本批。
3. helper 仍在可信 ChildRun 接入前拒绝；V1 实际 Runner、平台来源与 MCP 协议宿主由后续接缝对齐。新 HTTP 工具适配与 REST 已共用本批处理器。
4. 本批不实现 unknown 的自动重发或替换 call_id。后续来源查询必须关联原业务 operation，并保留其真实调用记录。
5. 原树旧 automatic/设置 WIP 不随本批提交，原五个 automatic 行为失败不改断言掩盖；上述门禁属于干净候选，未声称原树全绿。使用 50 文件自有补丁精确暂存，提交由主代理核对后批准；未读取 `.env`，未推送、合并或部署。

## 6. 提交划分

建议单独提交 `feat: add versioned announcements and verified flag submissions`，包含本批 21 个源码/测试/迁移文件、27 个合同/API 生成物和 2 份任务文档，共 50 文件。原有 WIP、P1/P3 独占目录与部署材料保留各自范围；自有补丁及双侧文件摘要留在本地审计目录，便于核对精确暂存。

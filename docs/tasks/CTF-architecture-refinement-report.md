# CTF 信息闭环与逐题派工改造报告

日期：2026-10-28。依据用户认可的[架构方案](CTF-architecture-review-20261028.md)，本次交付第一阶段及第二阶段的逐题派工部分；后续独立阶段明确列于文末。本报告不把离线协议改进当作已经实测的解题提速。

## 已完成

1. **模型可读任务索引。** list_challenges显式summary视图，owner/status过滤、绑定task与筛选的keyset cursor、完整条目字节预算。默认6000字节，1024–16000可调。旧长显示字段明确缩略，内部ID、版本、状态和继续读取信息完整，题面按ID补读；旧网页full与offset接口保持。
2. **索引和回执完整进入MAF。** 仅真实对应内建调用、有界且结构正确的summary/batch结果保留完整视图；伪造文本和超限返回不获豁免，整体模型输入硬限制仍有效。旧Session的预览决定不修改。
3. **逐题派工与小批量提交。** Lead新增create_challenges/assign_challenges，每批1–20项、每项明确request_id，逐项事务/CAS/幂等回执。明确4xx领域错误单项报告，503及未知基础设施失败传播，已提交项保留供原ID重试。external_id在同task锁内防新建重复，不按标题猜测重复，不改历史旧值。
4. **持久题目引用。** CtfMessage新增nullable challenge_id，派工与消息可引用本任务题目，沿用已有权限检查。迁移0020只新增字段，旧NULL消息兼容。复用现有MAF消息注入，不另建队列或工作单元状态机。
5. **重要成果自动反馈。** breakthrough/result、candidate、verification以及完成solution同事务写记录与确定ID通知，投递给Lead及相关owner/协作者。普通attempt不广播，已有求助不重复通知；终态、closing及成员停止保护保持。迟到验证明确记录summary_applied及记录结果与当前任务板状态，不能把过期accepted误当当前状态。
6. **渐进启动与自主求解。** 通用提示词要求独立验收题逐题登记、必要前置检查满足即派首批、持续盘点全源范围，队友自主推进并按题交付。未写入特定题库、靶机或漏洞套路，不根据文本自动拆分旧任务包。
7. **配置能力校验。** 新任务创建、角色设置保存和显式profile发布校验详情/全文读回工具。旧profile仍能加载、读取和恢复；角色设置可逐个修复，不因另一旧角色缺工具而无法保存。dry-run发布报告缺失能力，apply拒绝发布缺失能力的新版本。不会绕过白名单自动加工具。
8. **配置界面。** CTF角色设置沿用已有组件，Lead工具列表可选择两个新批量工具。OpenAPI、JSON Schema及前端类型由现有生成器同步。

## 复用与设计记录

复用MAF Agent/Session、原生工具、MessageInjectionMiddleware、已有turn、持久邮箱、数据库CAS、事件通知和执行fencing。没有新增依赖、独立消息服务、工作单元状态机或通用资源池。

已先更新CTF独立设计提案并单独提交 `0a5cf68`。原三份通用黑板设计未改。GPT-6 Astra子代理分工实现与独立交叉审核，主会话整合并补充配置合同、界面与验证。

## 实际验证

- 改造前基线 `make check`：2416 passed、419 deselected。
- 新能力、HTTP边界和相关已有离线回归通过；独立审查推动修复了非法筛选导致500、迟到验证状态混淆，以及主会话发现的批量503被误报为单项确定失败。
- 真实PostgreSQL专项14项通过，使用临时testcontainers与随机端口；涵盖commit/NOTIFY可见性、多记录/多收件人唯一约束、通知写失败整事务回滚、停止锁竞态、external_id并发去重、批量部分成功/CAS和0020旧数据NULL迁移。没有连接业务数据库写入。
- 前端 `pnpm --dir web check`：155项通过；生产构建成功。
- 最终 `make check`：**2463 passed、424 deselected**，Ruff/Pyright全绿。
- 更广的黑板服务CTF集成回归：**220 passed、281 deselected**，真实PostgreSQL临时实例，耗时约109秒。
- 全量过程中修正了两套旧成员重分配测试：新增完成通知后邮箱不再为空；测试现明确区分完成通知和责任转交通知，并实际验证pending stop、stopped、removed成员均无法claim新turn，generation/current_turn_id不变，重试不增加通知。该修订经过独立Astra审核，未为满足旧断言修改生产停止逻辑。

复现命令：

```sh
make check
.venv/bin/pytest -q services/blackboard/tests/test_ctf* -m 'integration and not live'
pnpm --dir web check
pnpm --dir web build
```

## 偏差、后续阶段与上线边界

- 可信资源状态适配及其等待反馈尚未实现。当前外部操作多为任意shell命令，没有现成的可信ready/busy/unknown合同；本轮只完善通用提示与信息传递，不从任意输出猜测资源状态。后续按认可方案扩展明确的typed adapter，不建立通用资源池。
- 分角色模型策略、异步长作业、稳定connector仍属于第三阶段，需分别设计生命周期和预算一致性验证；本轮没有调整effort、输出额度或执行超时。
- 延迟目标、首个可信结果时间及实际解题率尚未做真实模型对照。没有续跑已停止的任务，也没有调用真实模型测试。
- 现有平台配置不会因仓库模板改变自动更新。要使用新批量工具，需显式发布包含它们及全文读取工具的配置；旧任务继续使用冻结快照。不得直接把旧9个任务包改成63个完成状态。
- 初次交付检查点尚未合并或部署；用户随后授权替换本地并更新GitHub，完成情况见下节。公网环境本次未更新。

建议提交划分：`feat: preserve complete CTF board indexes and assignment scope`（合同、迁移、索引及MAF）；`feat: dispatch CTF challenge batches and notify key progress`（批量、事务通知、提示词与测试）；`fix: validate CTF readback capabilities before publication`（配置校验、界面与相关测试）；文档单独提交。前序模型与成果树未提交改动需保持独立，不能整树混入本次提交。

## Git发布与本地部署（2026-10-28）

用户授权后，先将前序已部署的模型配置、localhost、Responses续传及整理Agent修复单独提交为 `4bc9473`；本轮CTF实现提交为 `9dec897`，设计提交仍为 `0a5cf68`。已快进合并main并推送 `yimingy72/blackboard-explorer`。分组从前序部署快照精确提取，未覆盖已批准设计，也未提交私有运行数据。

本地blackboard与agent-runtime已替换为 `ctf-architecture-20261028`（源码 `9dec897`），数据库迁移从0019至0020。发布只在确认无活动任务/复盘/整理作业后进行，并保存私有数据库备份与回退镜像引用。

- 迁移前后22张业务表的原字段内容摘要一致；新challenge_id列全部为NULL，旧消息未被改写。
- 23个任务、191份会话、88639个对象（2182047052字节）及原任务预算/配置/归档保持；Postgres和MinIO容器、卷未替换，应用环境和端口相同。
- 随后按双版本CAS发布 `ctf v18→v19`、`ctf-task-settings v14→v15`。原模板与前序仓库模板完全一致，明确替换为本轮模板；Lead追加两项批量工具，两角色补齐全文读回。原有工具、模型、价格、参数、执行资源、MCP配置均保留，旧版本与所有任务快照校验不变。
- 登录页HTTP200，实际设置接口读回v19及所需工具；对旧9题任务进行只读索引查询，1KiB预算下3页完整返回9个唯一ID。任务保持stopped，没有模型调用或任务续跑。
- 私有备份、前后摘要及发布审计位于忽略目录 `.data/ctf-architecture-release/`。此次Git和本地发布不包含后续资源适配、角色模型策略或异步长作业。

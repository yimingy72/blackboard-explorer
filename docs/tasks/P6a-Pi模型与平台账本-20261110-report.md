# P6a · Pi 模型与平台账本交付报告

## 1. 完成内容

1. 按用户已批准的 [P6 方案](P6-Pi原生模型与MCP迁移-20261110.md) 先同步概念、实现、开发、Pi 运行时四份设计及交接说明。独立设计提交 `8685231a74ad1af365b74d1258aa39c9924f9259`；原批准方案与审计以原字节随设计提交保存。
2. 主站现有 `platform_configs` 成为 P6a 模型目录和加密密钥唯一权威，指定账号操作。公开 HTTPS 供应商由主站操作者保存授权，保存核对公网 DNS，可信发送时仍核对并固定实际连接地址，拒绝重定向和任意客户端目标。部署允许列表可选收窄；首期不支持私网供应商。切换上游 origin 不沿用旧密钥；已有冻结版本不改写。两站可通过独立受信身份读同一权威；通用服务令牌不能取出模型密钥。
3. 两条 Pi 链路复用既有 SDK 与受信 Runtime 发送侧，不新增对外模型网关或供应商流解析。实际发送前所属站 PG 原子核对原用途、逻辑调用、冻结模型/价目和当前权限，绑定实际请求摘要并许可一次。累计阈值保留多席位与已在途越线。旧账、未知证据和金额币种不重算；旧域收据对 Pi 任务仅更新 token，不重复记钱。结算支持同原调用晚到证据；完成但费用未知与发送结果未知分开。
4. 求解、成果整理与用户主动只读复盘分账，后者不设金额上限；各站用户的复盘读数按站点合并，缺站显示覆盖不全。模型管理、任务创建、账单与 Agent 视图使用原平台账号；缺价模型不冒充可用于带额度任务。新链不因错误回退 AgentGateway；旧冻结来源仅有限兼容，历史 UI 不被新金额覆盖。

## 2. 如何验证

- 最终候选 `make check` 全绿：Ruff、Pyright 0 错误、5 项 Pi 检查及 2,574 项 Python 测试通过，492 项按默认范围未选（Python 227.18 秒）。最终七个导入路径均位于候选。前一次普通检查发现四组旧测试仍替换调用模块已移除的导入符号；只更新 mock 指向实际共享旧工厂，原行为断言保留，定向 96 项通过（17.21 秒）后再次通过完整门槛。
- 设计基线在独立源码副本显式 `PYTHONPATH` 运行 `make check`：Ruff、Pyright、5 项 Pi 检查和 2,568 项 Python 测试通过，487 项按默认范围未选。设计基线记录了五个 workspace 包和 `eval.runner` 指向候选源码；最终检查另核实 `bbx_sandbox_execution` 也来自候选。
- 在候选目录，以候选 `packages/{contracts,objects}/src`、`services/{blackboard,agent-runtime,envd,sandbox-execution}/src` 和仓库根设置 `PYTHONPATH`，执行 `.venv/bin/pytest -q -m integration services/blackboard/tests/test_pi_model_ledger_integration.py services/blackboard/tests/test_pi_model_sender_integration.py`。只用可丢弃 PostgreSQL、受控 Runtime 和合成 HTTPS 供应商。守卫包括通用服务拒绝、凭据不跨源、真实 Task 创建与实际发送边界并发、已在途越线、未知保护、原账幂等和旧逻辑调用不能重发。定向合并 5 项通过（18.05 秒）：HTTP 实际领取的复盘、成果整理各用同一 TLS 模拟供应商实际发送一次，原 SHA 与分账持久；另有真实领取和构造证明缺价不限额复盘可完成、费用未知且不进求解额度。价目修订后的复盘仍优先原稳定模型 ID。普通额度追加 A/B 后重试 A 只累计两次，异值拒绝；锁内拒未决续跑，原调用确认后跨新一轮仍累计 USD 1.4，且旧逻辑调用不能重发。
- 运行验证另覆盖 Pi 1.4/1.5 的主回合、真实工具、分支摘要、压缩、hosted helper 和已发送丢回执不重发；Runtime SDK 定向 5 项通过。Chromium 验证平台注册缺价模型、补价后创建冻结 revision 的任务，显示未知/覆盖不全、无旧 AGW 模型选择，并检查窄屏布局。 相关 Web 定向 Vitest 15 项通过。
- 生成物复现：在同一候选环境执行 `make openapi && make web-types`；Runner Schema 用 `.venv/bin/python -m bbx_contracts.runner` 生成。构建和测试没有使用原 `.env` 或真实模型。
- 隔离接线使用 `.env.example` 的 Pi 项：每站独立 `PI_SITE_ID`、共同 `PI_MODEL_AUTHORITY` 和明确 `PI_MODEL_PRIMARY_SITE`，仅主站设 `PI_MODEL_OPERATOR`，副站经可信 `PI_MODEL_PRIMARY_URL`。`PI_MODEL_RUNTIME_TOKEN` 只在匹配的 Blackboard/可信 Runtime 上，`PI_MODEL_AUTHORITY_TOKEN` 只在控制服务之间。可选 `PI_MODEL_ALLOWED_ORIGINS` 只额外收窄公开目标，留空不要求为每个上游改部署。两站复盘互读以 `PI_REVIEW_PEER_SITE`/`PI_REVIEW_PEER_URL` 确认对端；未配置/不可达显示不完整。

## 3. 偏差与待决

- P6a 接通任务内模型、成果整理与用户只读复盘。本轮未提供脱离 Task 的平台模型测试或聊天演练；P6c 关闭旧入口前必须补齐原平台账号可用的独立测试，使用同一 vault 和明确的演练用途，不恢复网关虚拟密钥。
- P6a 是模型与费用切片。Native Task 的原 AGW MCP 和旧数据兼容仍在，后续 P6b/P6c 才替换、切流及移除。未启动、停止或修改现场部署，未导出真实凭据或迁移现场价格/历史费用。未证实的中转价格不能用官方基础目录或同名模型补造。
- 首期只明确支持已验的 `openai-completions` 公开 HTTPS，使用 Pi 根据冻结价目算出的估算金额，不宣称已支持供应商发票级实报；其他 Pi 协议、私网模型和可证明硬上限均不宣称交付。累计阈值不是硬封顶。旧暂停任务尚未做现场迁移；原冻结任务、旧账、SQLite/PV 与未决状态保留。
- 应用的工作树创建未返回。使用已有干净归档副本及独立 Git 索引/分支引用，原工作区 `HEAD`、索引、102 项工作和上轮两份 P6 文档的原状态、字节、模式不变。只允许测试库和临时 TLS 供应商，不推送、不合并主线。

## 4. 下一步与提交划分

本切片的合成数据验收已完成。下一步在同一隔离源推进 P6b 的双目标独立 MCP。P6c 在两站核清原资源和历史后切流，并只读保全原存储。设计提交 `Document the P6 Pi model and MCP transition`；产品提交 `Use trusted Pi calls and a site-local model ledger` 包含共享合同、数据库迁移、服务、Runtime、Web、生成物、必要测试和本报告。共享配置仅追加 `.env.example` 与 Compose 的 Pi 配置透传；两套 Pi 版本未变，Runtime 增加锁定的 `undici` 用于实际连接地址固定。

# 成果整理失败与问答阻塞修复报告

日期：2026-10-19。基线 `main@7753b4f`，工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `fix/progress-curator-response`。

## 现场与根因

公网任务 `69c3b499-50b9-4a41-b2ac-abec5e5b9b50` 的首次自动整理于北京时间 11:08 抛出 `ModelStreamError`，整理暂停；后来一个手动刷新和一个提问仍留在 queued。旧日志只有异常类名，无法还原这笔历史请求的确切终止原因。已确认整理消费 ¥0.06660752，未知执行预留 ¥0.313574，独立额度 ¥2；不是整理费用用尽，也不是整个模型服务不可用。

增加诊断后，在同一任务执行一个全新的整理提问，提供方明确返回 `response.incomplete / max_output_tokens`。请求使用原模型的 `max` 推理强度，输出上限已提高到 32768；返回用量为输入 30854、输出 32768、其中推理 29747。实际可见输出约 3021 token，响应不完整，不能发布树或执行部分工具调用。此笔响应的完整用量已正确结算，确认消费增至 ¥0.34384680，原未知预留仍为 ¥0.313574。该结果证明新请求发生输出截断，但不把它倒推成历史请求已确认的原因。

普通求解 Agent 没有走整理角色显式设置的 8192 上限；整理又继承了模型的 max 强度，因而“其他 Agent 正常”与整理失败并不矛盾。[DeepSeek Responses 文档](https://api-docs.deepseek.com/api/create-response/)明确输出上限包含推理 token；[推理模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)说明 medium 实际映射为 high。

## 修复

- 复用既有内容脱敏诊断，保存类别、阶段、终止原因、响应 ID 等有限字段，不保存错误响应体、密钥、提示词或部分模型输出。
- 响应明确失败但含完整一致用量时，先持久化失败收据再按实际用量结算；租约恢复识别该收据，不重放模型，不把失败响应当完成或发布候选树。缺失或不一致用量仍保留预留。
- 整理失败或租约失效后，尚未开始的刷新和提问同步结束，明确提示尚未调用模型，不再永久排队；认领提问后标记 processing。
- 新的手动请求可以显式续办。事务内核对旧 failed 作业、未知账本和 Session 活动标记，用 Session revision CAS 退休匹配的旧标记并记录审计；保留旧 unknown 金额及全部历史，不重跑旧 ID。未知费用存在时不会自动刷新，只允许新的显式请求；新预留仍受 cost + reserved + unknown 的原限额约束。
- 首次从单个目标节点生成成果分支时自动展开；后续更新保留用户的手动折叠。
- 成果页合并重复提示，区分确认消费与待确认预留，显示具体终止原因；失败事件即时刷新消息状态，保留失败消息和重新编辑入口。
- 整理设置增加独立推理强度和单次输出上限；默认仍继承模型，输出默认仍为 8192，允许按提供方能力选择上限。只覆盖整理角色，不修改原模型配置或求解 Agent。

共享合同新增 `ProgressFailureDiagnostic`、`ProgressFail.diagnostic` 和 `ProgressSettings.reasoning_effort`；同步 JSON Schema、OpenAPI 和前端类型；README 补充新设置和显式续办。扩大整理输出合同上界至 393216，实际请求仍受所选模型、上下文和独立预算约束，不给其他提供方假设相同能力。

## 验证

- `make check`：2267 passed、416 deselected，ruff、pyright 通过；无真实模型调用。
- `.venv/bin/python -m pytest services/blackboard/tests/test_progress_integration.py -m integration -q`：25 passed。覆盖旧 ID 幂等、未知费用保留、账本不匹配拒绝、旧租约拒绝、显式续办、失败收据结算及不能发布。
- `cd web && pnpm check`：155 passed，eslint、TypeScript 通过；`pnpm build` 成功。
- Playwright 成果树与配置持久化回归 9 passed；首次发布自动展开、手动折叠保持的新增用例 1 passed。
- `antd lint src/progress/ProgressPage.tsx --format json`：0 问题；`git diff --check` 通过。

真实公网验证分开记录，不混入离线测试：32768/max 的探针明确复现输出截断，并验证失败完整用量结算和未知费用不增加。后续 high/65536 的最终真实验证成功：新提问 `53386e0e-b483-4d26-8286-a49cd1860429` 完成、网页可见回复，发布 revision 1、11 个节点，截止 v61045。3 笔调用均已结算，整理确认消费 ¥0.65440352、原未知预留仍为 ¥0.313574、独立上限仍为 ¥2。后续来源仍显示待整理，不声称已覆盖整次任务。

## 数据与发布

先对 `69c3b499` 两个未执行的排队请求做定点备份并结束等待，断言求解状态、usage、预算、run_number、last_change_version、整理账本及 Session 不变。发布屏障又发现 `0ef17097` 一个相同的未执行遗留提问；该任务整理已因 TimeoutError 暂停，同样备份并结束等待，未启动模型、未清费用。

首轮发布在所有求解任务自行结束后进行；两端数据库 22 张业务表逐表摘要一致、MinIO 清单一致、环境变量和数据容器未变。本地保留 19 任务、150 会话、58663 对象；公网保留 24 任务、237 会话、120066 对象。发布没有中断求解任务。包含推理设置的版本已更新两端，并再次核对全部业务表与对象清单一致。当前任务仅覆盖整理角色为 high/65536；原求解模型与求解预算未改。

最终页面补丁也已同步两端。重新打开公网真实页面，核实 `11 / 11` 节点默认展开、真实回答可见；该次只读检查没有发起模型请求。当前发布标识为 `curator-response-20261019`。

审计和部署材料在忽略目录 `.data/progress-curator-response/`、`.data/curator-response-release/`；公网敏感备份位于 `/opt/blackboard-explorer/private/`，未读取 `.env`。

## 偏差与待决

历史 ¥0.313574 预留仍未知；不伪造使用量、不认定未执行、不自动释放。新显式请求与旧请求严格区分，所以用户可以继续，而费用上界仍保守计入原限额。原任务的求解消费和预算保持原样，本次不续跑求解、不增加预算。

建议提交：`fix: recover curator failures without replaying unknown requests`（合同、运行时、后端及对应测试）；`fix: explain curator failures and expose curation limits`（前端、生成类型、交互验证及本报告）。尚未提交或合并。

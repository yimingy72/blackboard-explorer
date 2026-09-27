# Interactive-agents · 并发发布与持久会话

用户要求：运行中及时发布 Fact/Intent、减少接力式串行探索；可对指定 Agent 发消息；Agent Session 持久保存到删除任务。用户已确认结束后的继续对话为只读复盘，不自动重开探索。

## 实施规则

- 探索、derive、Close 的语义不改变。通过通用发布节奏提醒与有界的并行 derive 利用空闲槽位；不提高默认金额、不降低推理强度、不注入单个评测答案。发布仍由 Agent 判断并调用工具，代码不伪造 Fact/Intent。
- 新会话保存实际 MAF AgentSession、初始指令与工具历史；运行中的用户消息在下一轮模型调用进入上下文。只读续聊串行恢复同一个会话，不执行命令、不发布事实/意图、不修改原任务验收状态。
- 旧任务没有原生 Session 时允许基于黑板、回执和已有记录初始化复盘会话，并明确标记 legacy，不伪称完整还原。
- 对话模型费用单独记入消息；任务结束不会清除会话。用户明确删除终态任务才清除任务、会话、消息与归属对象，清理失败可重试。运行中任务不能直接删除。

## 存储与 API 合同

新增 `agent_sessions`（task_id/agent_id 主键、session JSONB、opening_instructions、origin native/legacy、revision、updated_at）；`agent_messages`（id UUID、task_id/agent_id、role、content、status、reply_to、claim_token、lease_until、usage、created_at/updated_at）。用户消息状态 queued/processing/delivered/completed/failed；助手完成记录 status=completed。所有写入先锁 tasks 行，再操作会话/消息；CAS 防止覆盖新 checkpoint。任务增加 `deleting` 标志。

| 接口 | 权限与合同 |
|---|---|
| GET `/tasks/{tid}/agents/{aid}/messages` | user/service；`{messages, session_available, session_origin, mode}`，mode active/review；可选 status=queued 用于 runtime |
| POST 同路径 | user；`{id: UUID, content}`，最多20000字符；相同id/正文幂等，写 agent.message.posted 定向事件，返回消息 |
| GET `/tasks/{tid}/agents/{aid}/session` | service；`{session, opening_instructions, origin, revision}`，不存在返回404 |
| PUT 同路径 | service；`{session, opening_instructions, origin, expected_revision, deliveries?:[{id,claim_token}]}`；保存 checkpoint 并原子标记当前已消费消息 delivered；初次revision=0 |
| POST `/tasks/{tid}/agents/{aid}/messages/{mid}/claim` | service；`{mode:active/review}`，校验当前Agent活动状态，返回`{message,claim_token}`；固定300秒租约，冲突409 |
| POST `/tasks/{tid}/agents/{aid}/messages/{mid}/complete` | service；`{claim_token, session, opening_instructions, origin, expected_revision, content, usage}`；原子保存session、用户消息completed及唯一助手reply_to记录 |
| POST `/tasks/{tid}/agents/{aid}/messages/{mid}/fail` | service；`{claim_token, error}`，保留失败记录，错误不含密钥 |
| GET `/conversations/pending` | service；返回待处理复盘用户消息（Agent inactive，任务非deleting），最多100项，含task_id/agent_id/id |
| POST `/conversations/recover` | service；新runtime获得PG单实例锁后调用，将旧processing归队并撤销旧claim token |
| DELETE `/tasks/{tid}` | user；终态/created且无活动Agent、无processing聊天时设置deleting，queued消息取消，返回202；重复调用幂等 |
| GET `/tasks/deletions` | service；待删除task id列表，声明路由置于`/tasks/{tid}`前 |
| POST `/tasks/{tid}/purge` | service；runtime停止本任务cleanup并销毁容器后调用，幂等清理MinIO任务前缀/报告/归档和所有DB行；失败保持deleting可重试 |

用户消息/交付/回复/失败用 `agent.message.posted/delivered/replied/failed` 事件通知UI，事件正文仅摘要，不进入Fact/Intent或last_change_version。Agent不能伪装用户发消息或访问service session。读取session只对可信runtime开放。

## 分工与检查

- storage 子代理：blackboard、contracts、objects、迁移、OpenAPI及测试。另为并行derive预留agent_runs `derive_from_version` nullable bigint / `derive_parallel` bool默认false；登记derive时记录当前最大Fact版本和是否已有活动Explore，无需新增register请求参数。
- runtime 子代理：runner、middleware、session/chat worker、clients、server、supervisor（删除协调）及测试。不要改scheduler decision/executor或prompts。
- 主代理：前端、并行derive策略/提示词/发布提醒整合、设计与文档、完整验证部署。不得读写`.env`；普通与集成测试不调用模型。真实闭环仅使用显式隔离命令及通用非危险任务。

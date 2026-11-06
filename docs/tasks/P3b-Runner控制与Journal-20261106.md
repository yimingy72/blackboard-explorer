# P3b：持久 Runner 控制与 journal 接线

2026-11-06。承接 P2b 60a8cb5、P3a 8461cb4；开发为 GPT-6.1 Sol / max。本批按主代理与协议审核人已批准的小批边界实施，不开放 V1 执行。

## 范围

1. 一个 0023 增量迁移、四张表：Runner owner 历史、冻结 binding、有限命令、原始回执。owner 以 `(task_id, epoch)` 保留历史，唯一 current 约束与 Task 锁确保单一当前权威；不复用 legacy ctf_control.epoch。
2. service 登记 owner/绑定、投递有限 prepare/input/abort/close/shutdown。只接受创建时冻结的 challenge-board-v1。binding 保存真实成员、Assignment owner/scope 与现业务 turn/generation 映射；缺 turn 拒绝，不造业务 turn，不建立第二 claim 或人工收件箱。
3. 独立 runner-control JWT 与 RunnerIdentity，绑定 Task/Runner/PodUID/epoch/token_version，不能成为 service Principal。service 注册和续租返回准确的 credential_expires_at（Unix 秒）及 no-store Token；owner expires_at、固定 Task hard_deadline、30 秒会话许可、JWT 到期分别表示各自边界。
4. owner 初次期限来自可信 Task 剩余额度，接管/续租不延长固定上界；60 秒 owner 租约只能由 service 续期，过期原 epoch 不能复活。接管保留原 owner 证据，并允许同一冻结 binding/SDK/PVC journal 在新 owner 下恢复。
5. 命令与回执按 owner epoch 隔离，保存 payload_json 及 HTTP body 的精确 UTF-8 原文/摘要。重启重放仍活跃 binding 的原 prepare，不能因 ACK 成功就丢恢复入口，也不能越过待投递 input 的序号。
6. journal 元数据 CAS 与原 ctf_messages claim/recipient/purpose/turn/generation/token 核验同事务；只有确认原 native input entry/seq 才推进 applied/delivered。accepted 不回退 applied，applied 回执可使用更新的已确认完整 cursor。instruction 同事务去重追加真实 turn.assignment_message_ids，不改 legacy checkpoint_revision，session_revision 保持 null。
7. 长轮询等待释放数据库连接；实际延长短许可时立即返回续期。单 binding 停止/失效只撤其许可、停止再投递并持久 close，不让健康会话一起停止；已下发但 ACK 未知的 input 原状态保留。
8. prompt 仅条件读受信服务发布的绑定角色文本，revision 相同 304；模型/tools/Skills/环境快照保持冻结。close/abort 与迟到证据不表示远端清理或 Worker 席位释放。旧 owner 的已登记 pending confirmation 只归原证据，不推进当前 cursor/input/许可；新 owner 自行核对 PVC 并以新 fence 确认。

## 接口与权限

- `/api/tasks/{task_id}/runner-control` 仅接受独立 Runner 身份：commands 长轮询、原命令回执、binding acquire、journal 确认/读取、published prompt 条件读。
- `/api/tasks/{task_id}/ctf/runtime/runner-management` 仅 service：owner 注册/续租/撤销，绑定注册/接管，命令投递及 prompt 发布。公开创建 body 不承接内部业务版本或 ready 声明。
- legacy start/claim/supervisor 保持 V1 禁用；本批没有模型预算代理、tools/admit、业务 MCP、K8s 创建或存量迁移。真实资源/readiness/lease/模型工具全部就绪前不放开执行。

## 必要验证

只做三组 PostgreSQL 综合场景：身份/历史/到期；真实 Node Runner 对本地真实 Blackboard HTTP 的 prepare ACK 丢失、同 journal 恢复、input applied/close；输入事务/单调状态、跨 binding 队列、prompt、停止及迟到证据。外部 MCP/模型保持随机本地端口假服务。定点静态与兼容检查后，经独审冻结，在干净候选运行一次根门禁；不读取 `.env`，不触发真实模型、靶场或部署。

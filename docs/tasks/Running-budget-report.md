# 运行中任务预算调整报告

日期：2026-10-17。工作树：`/Users/yym/bbx-wt/running-budget`，分支：`fix/running-budget`。

## 完成内容

1. 任务 `0ef17097-b431-49cf-b431-46d94c67f2e6` 未停止、未重启、未更换执行容器，运行状态保持 `running`、`run_number=2`。
2. 金额预算由 ¥50 增加到 ¥70；总时长预算由 120 分钟增加到 180 分钟。调度器从下一轮状态读取起使用新预算。
3. 预算调整通过带任务行锁的数据库事务完成，同时写入 `budget.updated` 事件（版本 `56563`）并发送任务通知。事件记录原预算、追加金额和调整后预算，保留完整审计链。
4. 新增运行中预算调整接口 `POST /api/tasks/{task_id}/budget`，请求体支持 `additional_cost` 与绝对 `max_minutes`；只修改普通黑板的 `provisioning/running/closing` 任务，不创建新运行轮次、不清空黑板、不终止 Agent。
5. `budget.updated` 投影与前端 reducer 同步支持可选 `budget` 字段；旧只含 `usage` 的心跳事件保持兼容。

## 验证

- 任务实测：状态 `running`，预算 `{"max_cost":"70","max_minutes":180,"max_concurrent_agents":20}`，任务继续产生事件和模型调用。
- `make check`：2150 passed、353 deselected；Ruff、Pyright 通过。
- 运行中预算服务集成测试：1 passed。
- 前端 reducer 测试：8 passed。

## 部署说明

当前公网任务正在运行，因此没有重启 blackboard 来加载新接口。即时预算已直接通过原子事件生效且不中断任务；新接口代码合入后，待当前任务结束或下一次安全维护窗口再部署 blackboard 镜像。runtime、执行容器、数据库和 MinIO 均未重启。

## 偏差与待决

- 运行中预算只能调整金额和绝对总时长，暂未在现有前端增加按钮；本次由受控管理操作完成。
- 没有修改 `docs/design/`、没有读取 `.env`、没有新增依赖或数据库迁移。

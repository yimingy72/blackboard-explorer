# P4d：原生预算同 key 增额探针

在仓库根目录执行。需要已准备的项目 Python 环境、Docker，以及固定的 agentgateway 镜像和本地 `python:3.12-slim`。

```bash
docker pull cr.agentgateway.dev/agentgateway@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b
docker pull python:3.12-slim
.venv/bin/python deploy/agentgateway/p4d/probe.py
```

探针使用独立 Docker 网络、随机回环端口、一个 agentgateway 1.7.0 实例、独立 SQLite 和一个脚本化假模型。
只生成临时测试凭据。结束时清理容器、网络和临时数据库，在 `.data/p4d-native-budget-probe/` 留每次运行的脱敏证据。

1. 通过原生配置 API 创建一把测试 key，使用终身 USD 预算和 Block 策略。两次调用累计 0.006 USD，
   超过原阈值 0.005 USD；下一次返回 `budget_exceeded`，没有触达假模型。
2. 向同一资源 UUID 的 `PUT /api/config/resources/llm.apiKey/{id}` 发送 `{"value": ...}`，只把阈值改为
   0.008 USD。代理丢弃成功回包，探针只读回确认：同一 UUID、名称和 hash，配置 revision 增加一次；
   运行中的预算仍累计 0.006 USD。不存在的 UUID 返回 404，未创建资源。
3. 原 key 再调用一次后，累计变为 0.009 USD 并再次拦截。确认 0.009 USD 已落盘后强制重启，
   新阈值、同一累计和拦截都保留。

配置资源 GET 只证明持久配置；`GET /api/budgets/status?apiKeyName=...` 才能验证运行中已加载的新阈值和累计。
阈值变化不要求预算状态的 `updatedAt` 变化。原生 PUT 没有调用方 CAS；本探针只有一个作者、一次在位写入。
看到旧阈值不能证明写入未发生。回包不明时不要重发或创建替代 key。

这次重启只验证已落盘的累计。P3c 已观测强制终止可能让未落盘的用量回退；增额不改变该边界。
本探针没有验证多副本、原生 PostgreSQL 配置库或完整平台恢复流程，也不访问真实模型或靶场。

固定源码：[更新 API](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/ui.rs#L710-L774)、
[运行配置重载](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/state_manager.rs#L235-L278)、
[累计计数与保留条件](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/http/budget/mod.rs#L58-L75)、
[预算持久化](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/http/budget/database.rs#L13-L87)。

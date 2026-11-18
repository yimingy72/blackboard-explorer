# Agent Runtime

黑板模式与 CTF 模式共用一个控制进程。它持有同一个 PostgreSQL 监督锁，按任务冻结的模式和资源配置启动 OpenSandbox、Pi 求解会话、网络及流量记录。黑板仍使用 Explore、Derive、Close 和纯调度函数；CTF 保留各自的成员及派工规则。

`bbx-runtime serve` 启动共同监督器。可信部署需要 Blackboard 的私有控制地址、原 PostgreSQL 锁连接、对象存储、验明的模式发布以及集群配置，见 [部署说明](../../docs/使用与部署.md)。`MAX_RUNNING_TASKS` 是两种模式共享的任务容量，独立于各任务的 Agent 并发上限。执行进程不持有供应商密钥或宿主机 Docker socket。

`bbx-runtime state --task <任务 UUID>` 查看状态；`bbx-runtime conclude --task <任务 UUID> --agent agent-1` 发出交接请求。两条命令只需 `BLACKBOARD_URL` 和 `SERVICE_TOKEN`；新求解从站点创建并启动。

Pi 调用前验证原任务、Agent、会话和授权，黑板增量从同一任务取回。平台账本持久记录实际用量；未知的模型费用和工具结果保留原身份，不自动重放。终态后保留已确认的原始会话、命令、流量和已登记材料。显式续跑创建新的执行轮，恢复原始附件和已登记文件；只读复盘继续读取原记录。

`make check` 使用假模型验证协议和业务逻辑。对共同控制器、启动/停止、归档与网络的真实验证使用部署者拥有的隔离环境，不需要用户的模型密钥。

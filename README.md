# 通用问题求解引擎

通过黑板式多 Agent 协作，处理有明确目标和完成条件、但求解路径需要探索的任务。领域知识来自任务输入，可用于资料核对、数据分析、假设验证、方案研究和软件诊断等场景。

任务提供 `goal`、`domain_context` 和文字验收条件。Explore 调查并提交事实与意图，derive 根据缺口提出新方向，Close 核对证据、裁定完成状态并生成报告。Agent 自行判断语义重复；费用、时间和并发由调度器管理，Agent 不接收剩余费用或时间数值。

通用 Profile 只规定协作、证据与收尾规则；测试任务的专属标准放在任务文件中。mini-shop 是评测案例之一，不是平台定位。

先将 `.env.example` 复制为 `.env` 并替换密钥占位符。运行 `uv sync` 安装依赖，`make check` 检查代码，`make schemas` 导出契约。`make up` 启动基础设施，`make down` 停止容器，`make clean-volumes` 删除本地数据卷。

设计正本见 [概念设计](docs/design/黑板式探索架构设计.md)，开发状态见 [HANDOFF](docs/HANDOFF.md)。真实模型闭环命令与任务选择见 [runtime说明](services/agent-runtime/README.md)；这些显式命令会产生模型费用，普通检查使用假模型。

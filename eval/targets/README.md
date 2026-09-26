# 评估目标系统

本目录提供三个内置目标：`order-service`（偶发故障代码诊断）、`attendance-reconciliation`（CSV 数据核对）和 `mini-shop`（软件审查评测）。它们是检验通用引擎的案例，不构成平台的内置领域规则。

在仓库根目录执行 `make eval-targets`，将素材生成到忽略的 `eval/targets/dist/*.tar.gz`。本地工作台如需访问这些素材，启动时叠加 `docker-compose.dev.yml`；该文件创建执行网络中的 `eval-targets` HTTP 服务。基础 `docker-compose.yml` 不启动这个服务。打包脚本只分发任务所需素材；参考答案及验证脚本位于 `eval/answers/`，不应放入任务输入。

具体目标、任务选择与收费边界见[评测任务](../tasks/README.md)和[使用与部署](../../docs/使用与部署.md)。

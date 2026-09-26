# egress-proxy

这是**可选**的执行容器出网代理。默认 `EXEC_EGRESS_MODE=direct`、`EXEC_NETWORK_INTERNAL=false`，Ubuntu 执行容器直接出网，不启动本服务；任务的 `egress_allowlist` 字段只记录需求。

需要部署级白名单时，在 `.env` 配置 `EXEC_EGRESS_MODE=proxy`、`EXEC_NETWORK_INTERNAL=true` 和逗号分隔的 `EGRESS_ALLOWLIST`，构建 `make image-egress-proxy` 并以 `docker compose --profile egress-proxy` 启动。代理是 TinyProxy，默认拒绝未列出的域名；HTTP CONNECT 仅允许 443 端口。白名单在代理启动时从环境变量生成，修改后需重建代理容器。它是部署级统一列表，任务字段不会动态改变规则。

切换模式需要先结束运行中的任务并重建 exec 网络。完整命令、与模型代理的区别及故障排查见[使用与部署](../../docs/使用与部署.md#网络模式与排障)。

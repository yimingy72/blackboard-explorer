# 固定版 AgentGateway 原生管理操作

从固定版 AgentGateway 1.7.0 `26f5aebe99a36a037fe54f8080a608aca5547e15` 开始，按顺序应用 [P5b 中文补丁](../p5b/README.md)、[P5d 聊天接线补丁](../p5d/README.md)，再应用 `agentgateway-26f5aeb-native-actions.patch`。沿用 P5b 的 Node、pnpm、冻结依赖和原版 Apache-2.0 许可证。构建 `ui/dist` 后，用 `native_ui_manifest.py` 生成清单，继续使用 `native_ui_relay.Dockerfile` 只更新管理入口。

```bash
git apply --check /path/to/bbx/deploy/agentgateway/p5e/agentgateway-26f5aeb-native-actions.patch
git apply /path/to/bbx/deploy/agentgateway/p5e/agentgateway-26f5aeb-native-actions.patch
```

部署配置不再使用 P5d 的单把密钥摘要：

- `BBX_NATIVE_PLAYGROUND_DATA_URL` 是既有原生数据监听的私有 origin，与原生管理监听同主机名、不同 origin。
- `BBX_NATIVE_PUBLIC_LLM_ORIGIN` 是用户真正可访问的公开数据 origin。本地是 `http://localhost:15814`，供原生客户端接线页显示。

在原生「虚拟 API 密钥」中新建或轮换自己的密钥；默认使用原生哈希存储，保存成功时只显示一次完整值。把它粘贴到「聊天演练场」，选择允许的模型后发送；明确选择原始存储的运营者密钥也可在界面选用。密钥由原生数据库保存，无须再部署入口。平台任务、目录和复盘密钥仍由平台维护，不能在此改动或用于演练。

完整操作矩阵和保护边界见 [P5e 任务计划](../../../docs/tasks/P5e-AgentGateway原生管理操作补全-20261110.md)。本轮不改变原生数据、MCP、管理监听或网关二进制。

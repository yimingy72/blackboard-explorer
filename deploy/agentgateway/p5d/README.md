# 固定版 AgentGateway 聊天演练场接线

在 [P5b 中文原生 UI](../p5b/README.md) 上应用 `agentgateway-26f5aeb-playground.patch`。补丁只触及固定 AgentGateway 1.7.0 的 `ui/`，复用其聊天表单和发送实现，未改动依赖或网关二进制。沿用 P5b 的上游 Apache-2.0 许可证。

在已应用 P5b 补丁的固定源码目录执行：

```bash
git apply --check /path/to/bbx/deploy/agentgateway/p5d/agentgateway-26f5aeb-playground.patch
git apply /path/to/bbx/deploy/agentgateway/p5d/agentgateway-26f5aeb-playground.patch
```

按 P5b 的固定 Node/pnpm 命令构建 `ui/dist` 并运行测试。继续使用 `native_ui_manifest.py` 生成清单、`native_ui_relay.Dockerfile` 打包现有管理入口。构建无需部署地址或密钥。

本轮先修正入口，部署不设置下述两项。原生聊天演练场会显示尚未配置演练专用密钥，模型、提供商及成本入口继续用原生管理登录。原生管理 API 的权限边界不变。

以后单独开通演练密钥时，受信的部署需同时设置：

- `BBX_NATIVE_PLAYGROUND_DATA_URL`：现有原生数据监听的私有 origin，与原生管理监听同主机名、不同 origin。本地的 15814 和 15815 两个宿主映射无需改变。
- `BBX_NATIVE_PLAYGROUND_KEY_SHA256`：独立演练密钥的 64 位小写 SHA-256 十六进制摘要。实际密钥由操作者从安全渠道取用，原生页面手动填写且不保存。

页面和管理请求继续使用原生 Basic 认证；同源聊天请求只用独立演练 Bearer，仍由原生数据监听执行模型和密钥策略。不能使用平台任务、目录或复盘密钥代替。两项环境设置必须成对；撤下两项即可关闭聊天转发。不要通过修改 CORS 或开放原生策略写入接线。

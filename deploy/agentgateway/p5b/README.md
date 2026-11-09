# 固定版 AgentGateway 原生管理中文化

此目录的补丁面向原生 AgentGateway 1.7.0 固定源码 `26f5aebe99a36a037fe54f8080a608aca5547e15`。取自尚未合并的 [PR #2593](https://github.com/agentgateway/agentgateway/pull/2593)（[eaiu/agentgateway](https://github.com/eaiu/agentgateway/tree/c13b1eb06fd3e966150d3b566ce36a2636ebcdf8)，head `c13b1eb06fd3e966150d3b566ce36a2636ebcdf8`）。上游许可证见 [LICENSE.upstream](LICENSE.upstream)。

补丁只触及上游 `ui/`。在固定版本上移植社区语言资源、切换与呈现调用；保留固定版较新的模型表单、密钥轮换和标识、策略与 API 路径。直接将 PR 的源码或 lock 覆盖到固定版不兼容。只新增 `i18next` 和 `react-i18next` 的固定依赖，未降级固定版 React/Vite。

## 从固定源码复现

在工作目录以外单独准备上游源码，避免混入正在修改的文件：

```bash
git clone --no-checkout https://github.com/agentgateway/agentgateway.git agentgateway-p5b
git -C agentgateway-p5b checkout --detach 26f5aebe99a36a037fe54f8080a608aca5547e15
git -C agentgateway-p5b apply --check /path/to/bbx/deploy/agentgateway/p5b/agentgateway-26f5aeb-zh-cn.patch
git -C agentgateway-p5b apply /path/to/bbx/deploy/agentgateway/p5b/agentgateway-26f5aeb-zh-cn.patch
docker run --rm --platform linux/amd64 \
  --mount "type=bind,src=$(cd agentgateway-p5b && pwd),dst=/src" \
  -w /src/ui node:24.22.1-bookworm@sha256:d862bb5d082c5ae0210ac07e8c0344cb8cf7bef6d78e9b8c50c179da923108ea \
  sh -lc 'corepack enable && pnpm install --frozen-lockfile && pnpm build && pnpm test'
```

结果是官方原生 `ui/dist`（资源前缀 `/ui/`），不需要用户模型或 API 密钥来构建。适配差量 SHA-256：`72b35bc0c2da0e75f06c1de791c9e1f7ccf59ca7e80fdae97a05ed574a6ed47a`。本次构建的 30 个相对资源路径和文件哈希经排序后，紧凑 JSON 的 SHA-256 为 `4421e749c176df0bba40dcc59ee13437bb0a2f614c491d73eddb994273e12997`；其中 `index.html` 为 `daa4d296c899225f161795790a2de234cb8c6c894caf21e541235acaf7f38147`。

## 使用

仍从平台的「打开 AgentGateway 管理」进入原生页面。右上角可切换 English / 简体中文。首次按页面语言参数、已保存偏好、浏览器语言、英文兜底的顺序选择；显式偏好可跨刷新保留。模型与提供商标识、密钥、费用金额、接口参数不翻译或重写。

配置入口仍是原生「模型」和「成本 → 自定义成本」。原「虚拟 API 密钥」保留查看；本站由平台管理密钥，现有写限制不变。此补丁不新增 Provider 管理产品、不改变 API 权限、配置资源或账本。

# Runtime-sudo · 同任务 Ubuntu 内提权与操作记录

日期：2026-09-29。用户选择保留任务共享环境、Agent 工作目录和共享目录，提供 sudo 高权限并记录操作；取消 private 目录及高权限辅助容器方案。

## 完成内容

- 每个 Explore 用户加入 bbx-agents 组，允许在同一任务 Ubuntu 内免密 sudo；普通命令仍以自己的用户执行。privileged=true 改为该用户通过 sudo 执行完整 shell，保留原参数和历史配置字段兼容，不再使用安装命令前缀白名单。
- 新执行容器保留 Docker 默认 capabilities，增加 NET_ADMIN、映射 /dev/net/tun；镜像包含 sudo、iproute2、OpenVPN。移除执行容器的 no-new-privileges；relay 保持原限制，不增加高权限容器，不开放宿主网络、宿主文件系统或 Docker socket。
- envd 命令审计记录 command_id、Agent、命令、目录、提权请求、起止时间、耗时、退出码及完成/超时/取消/启动失败状态；返回 execution 元数据，沿用既有工具记录持久化至 MinIO。sudo 原生日志补充真实提权的调用用户、目标用户、目录、命令和退出状态。
- token 保护的 GET /audit 导出固定命令/提权日志；拒绝软链接、非普通文件、缺失文件，超过32 MiB明确413。新health通过 runtime_audit=true 声明支持。归档前保存 toolcalls/<task-id>/runtime-audit-run-<N>.txt（JSON内容），在任务快照与manifest中引用，复用现有证据读取鉴权及任务删除前缀。读取或上传失败保留容器；旧容器/缺失容器分别明确 legacy_runtime/runtime_missing，不伪造日志。
- 普通和提权命令的超时/取消清理覆盖进程组，并处理创建进程时被连续取消的竞态。恢复归档和重复 /users 调用均确保原Agent拥有sudo。
- Explore提示词保留“共享文件先复制再修改”，并要求系统和网络变更后同步黑板；不新增private目录或文件发布框架。Worker配置页删除已失效的提权前缀输入，显示执行权限说明。

## 验证

- make check：733项普通测试通过，ruff format/lint和pyright通过；基线为729项。日志 /private/tmp/bbx-runtime-sudo-check.log。
- pnpm --dir web check：33项测试及ESLint/TypeScript通过；pnpm --dir web build成功，保留既有大chunk提示。
- make test-integration：69项通过、737项排除，190.64秒；包括普通/sudo/root shell身份、root子进程超时清理、TUN创建删除、并发审计、原生sudo退出记录、direct/proxy两种任务归档与恢复、恢复后sudo可用。日志 /private/tmp/bbx-runtime-sudo-integration.log。
- 审查补充修复了启动时连续取消的进程遗留风险、审计固定路径软链接/FIFO读取，以及审计对象URI与既有读取API的扩展名不兼容。对应回归通过。
- 本轮没有调用真实模型，也没有运行用户的靶场任务。

可复现命令（仓库根目录；构建代理仅用于当前宿主机环境）：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
pnpm --dir web check
pnpm --dir web build
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration
```

## 边界与偏差

- sudo能越过同一容器内Agent的文件权限。目录用于组织协作和减少普通命令误操作，不承诺对root强隔离；所有Agent的系统软件、后台服务和网络状态共享。
- sudo shell/脚本记录该次调用及退出，不逐条追踪其中每次exec。容器内日志不抵抗root主动篡改；平台已保存的命令记录位于容器之外。突发容器丢失可能失去尚未导出的原生日志，档案会明确说明不可用。
- 命令输出原有64KiB截断及全文文件路径保持；需要保留完整输出文件仍应登记Fact附件。此次不扩大普通工作文件的归档范围。
- 新权限只在新建或显式续跑创建的新容器生效；不就地重建已有任务，也不自动续跑或修改验收状态。
- 构建遇到镜像源HTTP下载缓慢、直接HTTPS缺少基础CA的问题；保留已有基础工具链层，在CA安装完成后使用HTTPS获取新增软件。本轮新增的sudo/iproute2/openvpn及依赖在Ubuntu main中，临时缩小索引范围以减少下载，最终保留完整软件源。未关闭证书或软件包签名校验。

## 文件与提交划分

- 设计与任务合同：c0a1f25、4b55756；同步三份设计文档的权限与提示词边界。
- 实现及验证：services/envd、runtime的EnvdClient与ExecEnvManager、相关单测和容器测试。建议提交 `Enable audited sudo within task execution environments`。
- 提示词/界面/用户文档与报告：default/single Explore、README、使用与部署、Worker配置页。建议提交 `Document sudo collaboration and update worker environment guidance`。
- 无新增Python/npm依赖、数据库迁移、预算或模型配置改动。镜像新增sudo、iproute2、OpenVPN；使用既有MinIO任务命名空间。

## 交付补充

待容器集成与本地部署完成后填写。

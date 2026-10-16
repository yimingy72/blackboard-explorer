# Linux 执行容器 AppArmor 配置

Ubuntu 等启用 AppArmor 的 Docker 主机，默认 `docker-default` 禁止 mount，即使容器已授予 SYS_ADMIN，`ip netns add` 仍可能失败。Docker Desktop 上通过的网络命名空间检查不能替代 Linux 主机实测。

`bbx-exec-netns` 以 [Moby 27.5.1 默认策略](https://github.com/moby/moby/blob/v27.5.1/profiles/apparmor/template.go) 为基础，保留 proc/sys 和跨策略进程保护，只增加 `/run/netns`、namespace 内 sysfs 与 DNS 文件的必要挂载规则。其他挂载继续拒绝，不修改 Docker 全局默认、不使用 `privileged` 或 `apparmor=unconfined`。

在执行容器所在 Docker 主机加载：

```sh
sudo install -m 0644 deploy/apparmor/bbx-exec-netns /etc/apparmor.d/bbx-exec-netns
sudo apparmor_parser -r -W /etc/apparmor.d/bbx-exec-netns
```

在 agent-runtime 的 Compose override 中设置：

```yaml
services:
  agent-runtime:
    environment:
      EXEC_APPARMOR_PROFILE: bbx-exec-netns
```

重新构建包含该配置项的 runtime 镜像，并重建 agent-runtime。设置仅作用于之后创建的任务执行容器；relay 仍使用原 `no-new-privileges`。未设置时保持 Docker 默认策略，兼容原本地环境；指定策略未加载时，Docker 拒绝创建容器，不退回无策略运行。配置名限 `bbx-` 前缀，不接受 `unconfined`。

执行 Agent 仍在命令启动前去掉 SYS_ADMIN；AppArmor 只允许可信 envd 建立网络空间，不改变工作目录、sudo、共享文件或流量归属规则。配置方法参考 [Docker 官方说明](https://docs.docker.com/engine/security/apparmor/#load-and-unload-profiles)。实际服务器部署及验证见 [Server-deployment 报告](../../docs/tasks/Server-deployment-report.md)。

# P4m 原生 CTF 网络验证

仅使用名为 `bbx-p4m-k8s` 的独立 kind 集群、固定的本地镜像和专用端口。假的 OpenVPN 对端在同一私有 Docker 网桥上，但不在 Kubernetes Pod 内；以真实的 Cilium `/32` 对端规则验证线路。HTTPS 配置源只使用生成的测试 CA 和客户端证书；源的实际连接地址使用本机非回环 IPv4，证书仅为测试域名生效，验证错误主机名确实拒绝。脚本模型不会访问真实模型、VPN 或靶场。

从干净候选运行：

```sh
.venv/bin/python deploy/kubernetes/p4m/infra.py
.venv/bin/python deploy/kubernetes/p4m/build_images.py
.venv/bin/python deploy/kubernetes/p4m/probe_public.py --mode both --source-ip '<本机非回环 IPv4>'
```

`both` 在同一运行中的 API、Runtime、发布和对象存储上创建两个独立任务。第一个任务在 Lead 选择默认网络后执行并归档一个实际 TCP 命令；出口容器保持待机，没有连接凭据、领取操作、租约或外层 VPN 连接。原任务正停止后，第二个任务从同一公开入口创建，Lead 从真实 HTTPS 获取配置；由测试服务器分配隧道地址。Worker 从模拟题目接口读回另一个 IP 和端口并实际连接。随后验证同一隧道暂停恢复、新来源归属、原 Lead 断开、四个容器正退出和原抓包读回。

集群与产品镜像源码摘要、创建回执、临时夹具和证据分别保存在 `.data/p4m-task-network-20261109/k8s/`。环境只读验收前置失败且确定没有创建 Task 时，`--resume-setup` 复用原私有服务并记录单独的验收结果；已创建 Task 的失败只能用 `--stop-only` 对原 Task 收尾。禁止重放原 VPN 配置或以清理代替正退出。旧交付镜像为只读基线。

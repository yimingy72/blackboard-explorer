本地验证只使用隔离 kind 集群、自签名测试证书、假模型和数字 IPv4 TCP 回显目标。不得把测试发布文件用于实际部署。普通原生任务继续使用既有直连流量镜像；只有冻结的 `openvpn-ipv4-tcp-v1` 任务使用专用 Sandbox 和 Runner 出口容器镜像。原 CTF 工具层直接复用，不重装工具。

集群管理员须先在明确允许的节点分配 `devic.es/tun`（固定设备插件参见本地夹具），并允许 Runner Pod 只设置 `net.ipv4.ip_forward`。Runner VPN 容器仅获 `NET_ADMIN` 和分配的 TUN 设备，无 `hostPath`、不使用特权模式。OpenVPN 临时文件只落在出口容器私有 8Mi tmpfs。Task 仍只有原 Runner、Sandbox 两个 Pod；用户或模型不能传任意 OpenVPN 配置。可信部署发布固定的私有凭据引用、身份、可连接目标和三个相关镜像摘要。本地设备探针的主机路径对照在嵌套 kind 中也能打开 TUN，但不是此部署合同。

独立 Linux 内核与 supervisor 检查（镜像构建后，容器内无外网）：

```sh
docker run --rm --network none --cap-drop ALL --cap-add NET_ADMIN \
  --mount type=bind,src="$PWD/deploy/kubernetes/p4l/kernel_probe.py",dst=/probe.py,readonly \
  --entrypoint /usr/bin/python3 bbx-p4l-vpn-sandbox:20261108-aead -I -S /probe.py
docker run --rm --network none --sysctl net.ipv4.ip_forward=1 \
  --mount type=bind,src="$PWD/deploy/kubernetes/p4l/supervisor_probe.py",dst=/probe.py,readonly \
  --entrypoint /usr/bin/python3 bbx-p4l-vpn-sandbox:20261108-aead -I -S /probe.py
```

完整本地纵切（`candidate` 必须是本次经审的干净源码目录，并安装锁定依赖）：

```sh
python3 deploy/kubernetes/p4l/infra.py
python3 deploy/kubernetes/p4l/build_images.py --candidate "$candidate"
"$candidate/.venv/bin/python" deploy/kubernetes/p4l/probe_public.py --candidate "$candidate"
```

夹具使用独立 `.data/p4l-native-vpn-20261108/k8s`，绝不使用默认 Kubernetes context。初始化和大镜像导入可耗数分钟；不要以进程超时重试原 Task 创建。实际 Task 失败时，保留原运行现场和 `joint/awaiting-diagnosis.json`，先读原对象。用户停止仍按原 Task 并等待原容器退出；拆除夹具卷是测试清理，与产品普通停止保留 PVC 分开。

先查可信控制默认路由及 DNS，后由 Lead 请求固定 OpenVPN。外层使用传统 TUN、内层使用 WireGuard；单独的数据策略路由表只按来源接管目标，不更改控制默认路由。记录目标可达、未授权端口拒绝、不同 Agent 来源边界、暂停后同一外部 OpenVPN 身份、断线/关闭与原容器退出；PCAP 在原 Assignment 网卡处获取（加密前）。只支持已批准的 IPv4 TCP 数字目标；不包含目标 DNS、UDP/ICMP、任意用户 VPN、真实平台接入、自动换证或自动重拨。

# G1 Kubernetes Assignment 停止接缝（2026-11-06）

root 已审核最小计划并授权，由 actual GPT-6.1 Sol / max 实现。只写新 `deploy/kubernetes/g1/` 与本任务说明/报告；不改 Runtime/BB/shared 契约、锁、已审执行包或旧 P1/G7 证据。18 路径 SHA、ownership 前置、拒写 stub 与报告均已获 traffic_image_review 最终独审通过，现获准按冻结范围独立提交，不 push/merge。

冻结摘要：单 Sandbox/一次 Controller 恢复的真实证据、原2项模板/归属检查及增量错误 endpoint 零写入 stub 保留；Ruff/Pyright/bash-n 通过。root 已核本批部署/代码均不在根 make check 覆盖范围，且未改共享依赖，复用 `522a09c` 干净候选的2615项测试/Pi 5项通过证据，不重复根或 Kubernetes 矩阵。

1. 新独立 kind v1.34.0/Cilium 1.18.2 集群，独立 kubeconfig、任务前缀与 2 CPU/6GiB 限制；官方 OpenSandbox 1.2.0 BOM 不升级。复用官方/CTF 缓存，节点实际空间足够后才导大层，不改 docker-desktop 或已有 CNI/集群。
2. 一个 Task namespace、一个 Sandbox Pod；固定 template 开 `--kubernetes-bootstrap-cgroup-v2`。真实 Pod UID 来自 Downward API，环境代次是可信模板参数；Token/签名用本地测试 SecretRef，保留官方 bootstrap/ID/init/secureAccess。权限沿已审档，不加 privileged/hostPID/hostPath/host cgroup namespace。
3. CRI 只检查已证明属于本批 cluster 的精确 PodUID/CID。原始环境仅在内存解析，不落盘/输出；private cgroupns 或 mount/STATX 等能力不支持即 unsupported 停止，不擅改 runtime/权限。
4. SDK 可信登记真实 Pod/env/nonce 后冻结 binding，少量合并断言低 UID/文件/caps/FD/netns；背景 setsid 后代执行整 Assignment seal/kill/populated0，确认 SDK 控制完整终态与原 nonce，不把 404 当清理完成。
5. 只一次 Controller Pod 恢复，原 binding 无新 solver 准入且原 scope unknown；新 PodUID/newnonce 显式登记，新 binding 有效。首次成功 Ready 就保存 PVC UID，替换/停止后直接比 UID 与共享文件，不从事件猜留存身份。
6. 仅必要独立脚本静态、模板/归属检查与此单 Pod 场景；不重跑大矩阵、真实模型、靶场、VPN。结束清本批资源/镜像/测试凭据，官方大缓存保留。若必须改执行包源码，先报 root 协调。

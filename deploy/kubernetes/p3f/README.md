# P3f：可信 Runtime 与真实任务资源接缝

本目录固定任务局部 kubectl 1.34.0（下载及官方 SHA 校验），只操作隔离的 `bbx-p3f-k8s` kind 1.34.0 集群；使用 `.data/p3f-k8s-20261106/kubeconfig`，不切换默认 context，不修改 docker-desktop 的 CNI。每次写前核 kind label、实际 API endpoint；Runtime 另核冻结的 kube-system Namespace UID。

复用已校验的 P1 缓存、Cilium 1.18.2、OpenSandbox release-1.2.0 官方 Chart/Controller/Ingress/Execd 和 G7 Server 补丁。不自建 Operator，不直接造 Sandbox Pod。此目录不重跑 P1/G1/G7 的完整矩阵。

本机原 Kali c79 镜像和私有 kali-mini 基底已不可用。本片从已审 HEAD 导出 Ubuntu 旧执行镜像 Dockerfile/源码，再构建现有 sandbox-execution 静态启动器；记录新 digest 和 `bbx-p3f-ubuntu-g1-20261106` 环境代次。这只验证 Ubuntu 核心工具与生命周期，不代表 Kali、Sage、angr、pwndbg 工具集合已恢复，也不改变最终受信工具镜像的配置入口。

```sh
python3 deploy/kubernetes/p3f/prepare.py
bash deploy/kubernetes/p3f/cluster.sh
python3 deploy/kubernetes/p3f/build_images.py
```

首次需按 `deploy/kubernetes/p1/versions.json` 拉取固定 kind/Controller/Ingress/Execd/Cilium 镜像；缺缓存可先执行 P1 的仅下载准备。工具镜像只从 HEAD 导出内容构建，禁止把原树的 envd WIP 复制进去。Node 镜像使用固定 Node22.21.1 和 Pi1.5 锁，构建输入为 HEAD 加本片 `bootstrap.mjs/Dockerfile`，源码 hash 与镜像 digest 写入 `build-source.json`。

共享控制链在真实 BB HTTPS 租户权威端点启动后安装：

```sh
python3 deploy/kubernetes/p3f/install_server.py \
  --tenant-endpoint https://TRUSTED_HOST_GATEWAY_IP:PORT/api/opensandbox/tenants \
  --resolver-secret-file /private/task-owned/resolver-token \
  --ca-file /private/task-owned/ca.pem \
  --ingress-host 127.0.0.1:FORWARDED_INGRESS_PORT
```

端点由产品 BB 实现，不用 toy tenant 服务替代。Server 发 `OPEN-SANDBOX-API-KEY` 和独立 `x-bbx-sandbox-resolver`；返回 namespace/ttl。官方 Chart 的 Server 配置由 post-renderer 从 ConfigMap 改为 Secret，resolver 密钥仅从私密文件注入。它不进入值文件、启动参数或日志。其它组件仍为官方部署，Server 保留 G7 固定补丁身份核验。

每 Task 裸 Runner Pod 为 restartPolicy Never，无 K8s token、无 workspace 卷；journal 与 Sandbox workspace 两个 PVC 分离且不设随 Sandbox 删除的 ownerRef。Runner Secret 创建时预置 CA，因为 Node 只在启动时读取额外 CA；后续一次发布 bootstrap 与 scoped credentials，凭据轮换不用 subPath。启动门先核实际 Downward Pod UID，再将原 bootstrap 快照写独立 tmpfs，才允许 main 打开 journal。Task、绑定、模型准入与一次 Sandbox 创建 CAS 由 BB/Runtime 管理，部署脚本不造第二份权威。

停止前观察原 Pod UID/容器 ID；只有原容器的正向 Terminated 事件才可证明退出。删除 ACK、对象 404、TTL 或 Controller 重建都保持 unknown，PVC 保留。原生 Sandbox TTL 在实际 POST 前按剩余硬截止时间加固定120秒清理窗口计算；业务与模型硬截止时间不延长。

实际组合必须使用最终 clean 候选 BB/Runtime，证据关联候选 SHA、镜像源码 hash、真实 Pod/PVC UID；原有 WIP 不进入该运行。共同验证结束、证据保存后才运行明确的私有测试集群清理：

```sh
python3 deploy/kubernetes/p3f/cleanup.py --destroy-owned-cluster
```

清理核原 Docker container ID、K8s Node UID 和 Namespace/PVC 归属，先保存每个 UID；发现未知 Namespace/PVC 则拒绝。该命令销毁本片的测试集群及其中已确认的测试卷，不是产品 Task 的普通停止流程。产品停止保留卷，归档与最终数据回收另有权威。

## 实际验证分段

首段真实组合使用 clean core-v3 和当时资源源码，实际 kubectl 为宿主1.36.1、Server为kind1.34.0：同一真实Task完成Node/Pi prepare、SDK journal确认、保存业务MCP结果、超过65秒的owner续期与私密Secret轮换、旧global fence拒绝。首段退出如实停在unknown，因为原watch用了kubectl get不支持的--resource-version；没有用删除ACK或404补成功。官方[kubectl skew约束](https://kubernetes.io/releases/version-skew-policy/#kubectl)为相对API Server±1 minor，故此首段不冒称匹配BOM的kubectl组合。

修复后固定局部kubectl1.34.0：产品改为原生raw watch GET，resourceVersion/fieldSelector/watch/timeoutSeconds在URL中，事件仅在内存解析并核原UID/CID、固定单容器scope与Terminated。旧Pod已不在时，保存的Pod RV或同UID Namespace早期RV仅用于只读历史cursor；410、无事件仍unknown。原Task通过该产品路径回放取得原Sandbox的真实exit143，随后原CR缺失/无替换Pod、Runner原CID退出及两原PVC UID均确认，才经正常cleanup action收口。同Task未重建、未手写DB proof。每段实际模块hash分别在本地evidence/cleanup-evidence中。

最后只做一个独立测试namespace、无Secret/PVC的简单Pod，用同产品解析器和DELETE UID precondition验证live raw watch正退出，并精确清理该Pod/ns；不把它算第二个Sandbox或Agent矩阵，不重跑65秒/费用检查。源码为watch_probe.py，窄证据在`.data/p3f-k8s-20261106/live-watch-evidence.json`。

本机kind不能解析Docker Desktop特有host.docker.internal；probe仅读取本机Docker host-gateway地址，将它作为显式HTTPS控制origin和证书IP SAN，Cilium只放行该IP的控制端口。不修改全局DNS/CNI或产品safeUrl。首轮post-render连续JSON解析与上述DNS准备失败均在创建Task前保留原始停点，未发生执行重派。

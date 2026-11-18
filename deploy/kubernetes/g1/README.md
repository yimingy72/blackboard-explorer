# G1 Kubernetes Assignment 停止接缝

只在独立 `bbx-archv2-g1-k8s` kind 集群验证一个真正的 Kubernetes Sandbox，随后仅一次 Controller 恢复新 Pod。目录不接入生产部署，不改旧 P1/G7 或执行包源码。

固定：kind v0.30.0、Kubernetes v1.34.0、Cilium 1.18.2、官方 OpenSandbox 1.2.0 Server/Controller/Ingress/Execd；工具层复用原 CTF digest，执行包来源是已审提交 `d5c84059c55864a84a3efdd09c4ab3477268a318`。单 namespace 不测 Server 规模，所以不重建 G7 overlay。所有工具/源包先校验 P1 已固定缓存的 SHA，再提取到本批 `.data/bbx-archv2-g1-k8s`；不改缓存源树、全局工具、默认 kubeconfig 或其他 CNI。

## 模板与可信边界

`batchsandbox-template.json` 是 YAML 的 JSON 子集，由官方模板加载器直接读取。固定主容器入口 `--kubernetes-bootstrap-cgroup-v2`：

- `BBX_POD_UID` 来自 Downward API `metadata.uid`；`BBX_ENVIRONMENT_REVISION` 是可信模板常量。模型请求不提供这两项。root 启动器自行生成 nonce，SDK 在可信登记时读取并冻结到 binding。
- Execd Token 使用同 namespace SecretRef，Server 与 Ingress 共享本地生成签名 Secret，保留官方 secureAccess、bootstrap、Sandbox ID 与 `EXECD_INIT=1` 路径。
- valueFrom 项显式带空 `value`，避免固定旧 Go JSON patch 对 slice 元素复用时残留非空值。运行 Pod API 只展示环境名称/引用，禁止打印 Token 或原事件正文。
- 权限沿原已审控制档：非 privileged，固定 capability ceiling、AppArmor/seccomp 例外；不加 hostPID/hostNetwork/hostPath、host cgroup namespace 或新权限。Task 工作文件用 64Mi retained PVC；root 控制身份不在 Solver 卷中。

先从本批精确控制 PodUID/CID 验证 CRI private cgroup namespace，再核最终 Sandbox 自己的 namespace、根和配置。CRI 原文含环境值，只在内存解析，绝不落盘或输出。若运行时不是 private，或 native 新档因 mount/内核能力拒绝启动，写 unsupported 并停止，不更改运行时或权限来强行通过。

## 运行

宿主复用 `services/sandbox-execution/.venv` 的独立 SDK 锁环境；本机已有经校验的 P1 kind/Helm/官方源包/Cilium chart 缓存。缺缓存时可先执行只准备缓存的 `bash deploy/kubernetes/p1/prepare.sh`。需要 Docker、kubectl 与足够本地磁盘；不读 `.env`，不调用模型、靶场或 VPN。

```bash
python3 deploy/kubernetes/g1/prepare.py
services/sandbox-execution/.venv/bin/ruff format --check deploy/kubernetes/g1
services/sandbox-execution/.venv/bin/ruff check deploy/kubernetes/g1
services/sandbox-execution/.venv/bin/pyright --project deploy/kubernetes/g1/pyrightconfig.json
PYTHONPATH=deploy/kubernetes/g1 services/sandbox-execution/.venv/bin/python \
  -m unittest discover -s deploy/kubernetes/g1/tests -p test_fixture.py
bash -n deploy/kubernetes/g1/run.sh deploy/kubernetes/g1/cleanup.sh
bash deploy/kubernetes/g1/run.sh
bash deploy/kubernetes/g1/cleanup.sh
```

节点限制 2 CPU/6GiB，API 绑定 loopback 随机端口。复用同名节点前先断言 kind ownership；节点建立/复用后，必须先确认 kubeconfig endpoint 对应本节点 API 映射，才可更新节点或执行任何 Helm/kubectl 写入。context 名称正确不够。先部署小控制组件、确认运行时，再检查节点实际可用空间大于 50GiB 才导入 CTF 层。只取固定 digest 的必要 amd64 镜像；Docker 29/containerd 导入保留 BOM root index，并创建正确 canonical digest alias，避免误拉其他架构或依赖旧 alias。

真实场景只有：低 UID/文件/环境/caps/FD/netns 的少量合并断言；背景 setsid/double fork 心跳→seal/kill/populated0；一次删除自建 Pod，由官方 Controller 恢复；显式新登记后旧 binding 拒绝、新 binding 可用；首次 Ready 保存的 PVC UID 与替换后/停止后直接比较。没有重跑整工具、NET_RAW、多人/多任务网络或 Server 规模矩阵。

创建与首个假命令前分别持久写 `create.attempted`、`verify.attempted`，同 invocation 不自动重发。中断时先看本批现场与原证据；原 CR 存在可恢复连接，已有命令标记会拒绝盲重验。不要删标记后重复 create。完整重现可先清理本批自建环境，再从空运行开始。

`cleanup.sh` 只删本批集群/空网络/自建 `bbx-archv2-sandbox-g1-k8s:20261106` tag 和测试凭据/kubeconfig；官方、CTF、Cilium 与工具缓存保留。原 attempt/PVC 基线移入本批 closed-runs 历史。可提交脱敏证据在 `evidence/20261106.json`；完整本地观察保存在忽略目录，Token 值和原始 CRI 环境不在证据中。

此结果只证明已测 Kubernetes 组合下的进程 scope 接缝和一次恢复，不代表完整 G1/G2、全网络隔离、VPN、在飞文件 worker、Agent loop 或平台席位回收。unsupported 与 unknown 不作为清理完成。

独审后的顺序修复仅新增一个隔离 stub：同名 context 指向错误 endpoint 时，确认节点/Helm/kubectl 零写入。它复制脚本到临时目录并替换所有执行入口，不访问真实 cluster；原单 Pod 证据沿用，修复不重跑矩阵。

# G1 Assignment 命令与后代停止证据（2026-11-06）

本批由 root 与 traffic_image_review 条件审核通过后授权，开发模型 actual GPT-6.1 Sol / max。先进行现有权限档内的 owned Docker 可用性探针，通过后才实现；修改仅限 `services/sandbox-execution/` 与本任务说明/报告，不改 Runtime、共享合同、Kubernetes 配置或其他任务 WIP。当前 15 路径源码/证据已独立审核通过，获准按冻结范围独立提交，不 push/merge。

冻结摘要：本包 make check 6 项、真实合并停止场景 1 项及旧身份回归 1 项通过，仅提供已测 Docker 命令 scope 的局部证据。主代理已核根检查范围不含本包，本批不改其覆盖输入/共享依赖，复用 `522a09c` 候选根门禁 2615 项与 Pi 5 项通过证据，不重跑根全套或集群。

## 目标与固定边界

- shell 非零退出、后台启动回执、取消受理与整个 Assignment 命令树排空分开记录。缺原身份、取消/清理响应丢失或无法观察为空时保留 unknown，本包不释放席位、不重派原 invocation。
- 沿用官方 OpenSandbox SDK/execd 1.2.0 和原 CTF 镜像。官方普通取消只覆盖原进程组；本批不迁移官方独立会话 API，不写新执行服务。
- 共同 UID/GID=10000、同 Task 共享文件、固定可信 basic/net-raw 档不变；模型不能选择 cgroup、身份、启动档、特权或额外环境。

## 审核后的最小实现

1. 新固定可信启动档在显式 private cgroup namespace 中，验证根是当前容器，建立 root 私有 cgroup2 挂载。只处理本容器的非根 scope，不改 controller、subtree_control、父资源限额，不用 privileged/hostPath/host cgroup namespace。旧档可继续无 accounting，但不能给出排空证明；新档 marker/context 存在或损坏时绝不退回旧 argv 入口。
2. 启动时生成不可复用的容器 incarnation；可信环境登记通过窄只读命令取得并冻结进 AssignmentBinding、scope key 与每次请求。不能在 run/stop 中悄悄刷新。即使同 Pod UID/原 envrev 重建容器，旧绑定也拒绝，不能创建新 scope 后冒充原命令已清理。
3. root 非 Solver 卷保存完整绑定、namespace/mount/boot/unique mount ID/device/inode、seal 和完成证据。短全局创建锁与独立 scope flock 保证创建/封口；等待 A 排空不占 B 的锁。launcher 锁内核当前完整身份及未 sealed，先加入且核实内核成员，再关闭控制 FD，沿原逻辑降身份并 exec，无新增 solver fork/PID 包装层。
4. cleanup 留在 scope 外，先 fsync seal，`cgroup.kill=1` 只记录动作；限时观察 `cgroup.events populated=0`，再次核当前原身份并持久保存完成才确认。ENOENT、裸 inode 同号、Pod/环境/incarnation 变化或重挂都不是 empty；原 scope 只可幂等重查，不复用名称。
5. 新档要求运行时支持 `STATX_MNT_ID_UNIQUE` 等实际内核能力，缺少即 unsupported/拒绝新档，不声称所有 OpenSandbox/Kubernetes 环境都支持。生产 Kubernetes 最终 Pod 留待后续独立门禁。

## 必要验证

只用本地假命令，不调用模型、靶场、VPN、`.env`。一个合并真实 Docker 场景覆盖：A 的前台 exit7、后台 setsid/double fork、B 同 UID 共享文件；cancel/cleanup 响应未知、并发/晚到 launch、原 scope 重查、旧/新 generation、原身份缺失与同 inode 新 mount；保留旧 adapter/journal，在同 Pod UID/envrev 的新自有容器上证明旧 incarnation 被拒、新登记有效。只补 SDK 干净 EOF 等必要负例与受影响旧身份检查，不跑大集群或全工具矩阵。保留已有 WIP/官方缓存，结束清理本批容器和镜像。

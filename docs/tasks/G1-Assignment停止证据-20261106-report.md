# G1 Assignment 命令与后代停止证据报告（2026-11-06）

状态：15 路径 SHA、代码和报告均已获 traffic_image_review 最终独立审核通过，获准按冻结范围独立提交。actual developer：GPT-6.1 Sol / max。变更仅在 `services/sandbox-execution/` 及本任务说明/报告，未改 Runtime、共享合同/锁、Kubernetes 模板或原 WIP。

## 1. 完成范围与依据

固定官方 SDK/execd 1.2.0 与旧 CTF 工具层不变。官方普通 [interrupt.go](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/execd/pkg/runtime/interrupt.go) 按原 PGID 发信号，并明确排空检查是 best effort；已退出的 shell 不足以找到所有 setsid/double fork 后代。当前 adapter 原有 foreground terminal、background ack、cancel unknown 与原 ID 观察保留，没有把 shell exit 或取消 200 改成整 Assignment 完成。

官方 `sandbox.isolation` 确实可配 workspace `rw`、共同 UID/GID 和 PID namespace/lifecycle drain；本批没有错误地把它视为必须逐 Agent 文件隔离。但迁移还需装配 bwrap/native gate、更换运行/后台/status/ID 路径，并重接现有 netns/proxy/NET_RAW。经 root/traffic_image_review 条件审核，本小批选择保留现成 SDK 路径的受信 cgroup 窄扩展，没有新执行服务或上游 execd 补丁。

已完成：

1. 静态启动器新增固定 `--execd-cgroup-v2` 档及窄 context/stop/status 命令。显式 private cgroup namespace，新挂载仅根于本容器 cgroup；root 控制区不在 Solver/PVC 卷。只做进程归属和终止，不写 controller/subtree_control/父限额，不增加模型可调特权。legacy 档不提供排空证明；新档 required marker/context 残留、损坏或缺 scope argv 时拒绝降级。
2. 新档读取可信 Pod UID/环境代次，并生成独立、不可复用的随机容器 incarnation。首次可信登记显式读 `read_execution_context` 并冻结到 `AssignmentBinding`、scope key、每次命令/控制请求。模型请求未增加身份字段，run/stop 不重新发现或替换 nonce。
3. root manifest 完整绑定，短全局创建锁与独立 scope flock；锁内复核环境/绑定/未封口，先写 cgroup.procs 并核成员，再关闭控制 FD、降共同 UID/GID/caps/NNP，沿原 exec 保持 PID/PGID/官方 ID。继承关系来自内核 cgroup，不用 `/proc` 时间邻近猜归属。
4. 整 Assignment 停止先持久 seal，cleanup helper 留在目标 scope 外；kill 成功只算动作，限时观察递归 populated=0、再次核原身份并持久完成才确认。原身份含 incarnation、Pod/env、boot ID、cgroup/mount namespace、STATX unique mount ID、设备/inode；ENOENT、同 inode 新 mount、缺记录或身份变化均 unknown。
5. journal 只增加不可变停止准入与观察事件，不另建席位权威。停止响应未知保留原 control execution ID；重复调用只 status，原模型 invocation/kill 请求不重派。本包不释放席位。已有同 Task 文件共享、低身份文件 API 无 root fallback、basic/net-raw 固定档保持。

内核机制依据：[cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html) 说明 fork 继承、cgroup.kill 对整子树及并发 fork 的处理、populated 对活进程的递归观察；[cgroup namespace 文档](https://man7.org/linux/man-pages/man7/cgroup_namespaces.7.html) 说明 namespace 根相对于创建者当前 cgroup。新档额外要求实际 `STATX_MNT_ID_UNIQUE` 支持；缺失按 unsupported/拒绝处理，不能外推到任意 OpenSandbox/Kubernetes 内核。

## 2. 如何验证与实际结果

已先保存执行包当前基线到忽略目录 `.data/bbx-archv2-g1-20261106/baseline*`，未改原 WIP。

**先行 owned Docker 探针通过。** LinuxKit 7.0.12 / cgroup v2，最终配置 cgroupns=private、privileged=false，仅沿原 P1 显式 SYS_ADMIN/NET_ADMIN 与 AppArmor 例外；新私有挂载根 `/`、根只含本容器 init。非根 scope 的 UID/GID10000 setsid 假 sleep 成功加入，kill 后 SIGKILL、populated=0，subtree_control 前后不变。探针容器已删除。首轮仅有 Docker CapAdd 返回 CAP_ 前缀/顺序的 harness 断言问题，规范化核对后同权限重验通过，没有提权。

从独立包目录复现：

```bash
cd services/sandbox-execution
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-assignment-cleanup
```

实际执行的必要检查：

| 检查 | 实际结果 |
| --- | --- |
| 本包 make check（frozen 独立锁） | Ruff 格式/lint、Pyright 0 errors；非集成 6 passed，6 deselected，最后约 0.29 s |
| GCC 静态编译 | `-Wall -Wextra -Werror -static` 通过，readelf 无 INTERP，无新增运行库 |
| 一个合并真实 A/B 停止场景 | 最后 1 passed，约 14.74 s；同场景内第二个自有容器覆盖同 Pod/envrev 重建 |
| 受影响旧身份/文件/非零退出回归 | `test_identity_files_foreground_and_output` 1 passed；同 Task 低 UID 文件共享、环境/FD/caps、官方 ID、exit7 保持 |

合并场景实际证明：A 前台 exit7 后 setsid/double fork 心跳仍活，且未产生 cleanup confirmed；普通后台取消实际执行后故意丢回执，cancel unknown 仍不冒充清理。整 Assignment 停止与新 launch 并发，后代被排空或晚到被拒；实际完成后故意丢 stop 回执，journal 先 unknown，再对原 scope status 重查恢复 confirmed，kill 请求计数仍为 1。封口后启动拒绝，B 与新 generation 继续心跳。共同 UID/GID10000、普通 caps 全零与 net-raw 仅 NET_RAW、控制环境不泄漏、控制 FD 关闭均通过。

身份负例：缺原 cgroup、篡改 mount 身份但保留 scope inode均不能确认；持 A scope 锁时 B 新命令仍能完成。真实同源 bind mount 替换后 cgroup inode 不变、unique mount ID 改变，原查询为 unknown。故障注入采用 bind 替换；直接卸载/再挂 cgroup2 在该运行时返回 EBUSY，本实现不依赖运行中卸载/再挂来工作。新档同时拒绝旧 argc 启动，context 变化不退 legacy。

容器重建负例：两个不同 Docker container 保留同一个受信 Pod 标记、同 envrev、同 Sandbox ID，旧 adapter/binding/journal 保留；新容器由启动档生成不同 incarnation。旧绑定先发新命令就被拒，不创建旧 scope；随后查询原命令仍 unknown，不能把新 scope 当旧 scope。明确重新登记的新绑定可运行/停止，旧 nonce 重查不会杀新进程。这里是 Docker 中的容器重建证明，不冒充真实 Kubernetes 重启验证。

证据位于忽略目录 `.data/bbx-archv2-g1-20261106/{docker-cgroup-probe.json,combined-evidence.json}`；只含本地测试身份与布尔观察，没有输出 Token。测试复用了官方 execd 与 CTF 缓存，没有真实模型、靶场、VPN、`.env` 或其他集群操作。未跑整套工具/大集群矩阵，未重复无关 root 测试。

主代理已核对根 Makefile 的 PYTHON_DIRS 及 pyproject 的 pyright/pytest 范围，均不包含 `services/sandbox-execution`；本批不改变其覆盖输入、共享依赖或锁。因此复用提交前 `522a09c3ef3b098cee474959ab21029e37610d46` 干净候选已通过的根门禁（2615 项测试、Pi 5 项），配合本包 make check 6 项、真实合并 1 项与旧身份 1 项证据，不重复根全套或集群。不把原树旧 WIP 声称全绿。

## 3. 清理、偏差与待决

- 所有本批测试容器由 try/finally/testcontainers 删除；自建 `bbx-archv2-assignment-stop*` 镜像在证据保存后清理，官方/CTF/P1 预存镜像保留。未改默认 kubeconfig、Docker 全局设置或现有 CNI/平台。
- **只闭合已测 Docker 新档下的 Assignment 命令 scope 无活进程证据，未宣称 G1/G2 整体通过。** populated=0 不承诺 `/proc` 无待回收 zombie；scope 不包括在飞文件 worker、外部 Agent loop、网络资源释放或平台席位状态。
- K8s 对应固定入口已编译，尚未在最终 Pod 验证 private cgroupns、可信 Pod UID/envrev 注入、内核权限/STATX 与重建登记；本批未改 Kubernetes 模板。不得从 Docker 外推，缺能力拒新档，不偷偷增加 privileged/hostPath。
- root manifest/空 scope/tombstone 保留至 Task 容器销毁，名称不复用。同一 writable layer 留有 required/context 时重复初始化拒绝自动覆盖；控制 daemon 恢复/外部已销毁环境的对账仍需后续生命周期接缝。
- 初版设计只比较 Pod UID/envrev，审核指出同 Pod 容器重建可保留两者；已通过新的启动 incarnation、不可变绑定、逐次 root 校验及真实重建负例修复，不能只文档要求 envrev 自动刷新。

## 4. 下一步与提交建议

traffic_image_review 已完成 nonce 登记/冻结、响应门禁、root 文件/锁及合并场景的最终只读复核，15 路径 SHA 与冻结候选一致，未发现新增阻塞。获准仅提交执行包自有增量 + 两份任务文档，标题 `feat(execution): prove sealed assignment process cleanup`；正文按 AGENTS，并注明 `Actual developer: GPT-6.1 Sol / max`。不 push/merge，不夹带其他 WIP。

后续另批将证据接回已有平台 fencing/ToolInvocation/席位账本，并在独立最终 Pod 检查其启动与重建；保持同 Task 文件和共同低 UID，不扩到 VPN 或全审计 UI。

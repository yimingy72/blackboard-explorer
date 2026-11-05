# OpenSandbox 执行接缝（P1）

这是独立、尚未接入生产路由的接缝包。执行、状态、后台输出、中断和低身份文件访问使用官方 `opensandbox==1.2.0`，不修改旧 envd，也不新建执行引擎、MCP 生命周期服务或平台状态机。

## 固定依赖与镜像

- Python 3.12/3.13；独立 `pyproject.toml`、`uv.lock`、`.venv`，不修改根 workspace。SDK 公开发布 wheel SHA-256：`15a0f3c3245018afa43b75fb14e892a64c2c8807276a0fdaf2a1a469f5e214ea`，其余依赖由独立锁记录。
- 官方 execd：`opensandbox/execd@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c`；自报 commit `25873227f60424bdc59bfa83b963bbc00e871297`。方案源码标签 `62a64f0b48cdd6b410f36e53c74dc7559c87719a` 比它多两个仅修改发布清单的提交，见 [官方版本记录](../../docs/tasks/OpenSandbox接入方案.md)。未重编或修改 execd。
- 工具层复用已有 `bbx-exec-env-kali@sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae`，追加静态启动器、官方 execd 和只读窄扩展。没有重装 Kali 工具。这个工具镜像目前是本机可用的 digest，集群发布仍需上传获准仓库并核验节点拉取。
- 启动器使用已有 GCC/glibc 静态构建，无新增运行库、setuid 位或 file capabilities。`tests/Dockerfile` 仅为测试附加恶意加载标记与本地 HTTP 目标，不进入工具镜像。

## 实际调用边界

可信调用者构造不可变 `AssignmentBinding`，绑定 Task、Agent/角色、父 Agent、题目、Assignment/generation/epoch、Sandbox、Pod UID、环境代次与网络身份。模型输入只能形成 `CommandRequest` 的命令、工作目录、前后台和有界超时；命令/调用 ID 由可信调用账本签发。模型不能选 UID、netns、capability、任意环境或 Sandbox。

`SandboxExecution.run` 先提交本机准入证据，再调用官方 `commands.run(list[str])`；命令文本是单独 argv 数据。官方 `on_init` 到达时立即持久绑定原 execution ID。前台只接受带退出码的 complete 或匹配的明确 error.exit_code 为终态；后台 execution_complete 只表示启动回执，分别记录。仅收到 init/部分输出后正常 EOF 也保留 ID/已收文本并记 unknown。状态与取消始终查原 ID；本包不会再次调用已准入 invocation。可信 `connect_bound_execd` 明确关闭通用/wake 重试；外部注入的 Sandbox/transport 不在这个重试配置保证内。连接关闭、取消受理或 shell 退出均不冒充全后代已排空。

启动器先进入固定 netns，关闭标准流以外的继承 FD，再清空控制环境、清 supplementary groups、降共同 `UID/GID=10000`，设置 no-new-privs，最后 `execve` 固定 Bash，不额外 fork，不改变官方 PID/进程组。原 PID、PGID、进程启动 tick 与 netns inode 写入 root 控制区，用于与 execution ID 的可信证据关联。

只有两个受信环境档，属于 `AssignmentBinding.execution_profile`，不属于模型命令参数：

| 档 | Solver 能力 | 实际用途 |
| --- | --- | --- |
| `basic` | 所有能力清零 | 一般命令与普通 TCP/UDP |
| `net-raw` | 仅 NET_RAW 保留在 bounding/permitted/inheritable/ambient 集合 | CTF 原始报文；仍无 SYS_ADMIN、NET_ADMIN、SETUID、SETGID、SETPCAP |

固定 `--execd` 启动模式在 daemon 的初始化探针之前重建控制环境，仅保留执行端 Token、受保护环境文件与固定 PATH/HOME；空 Token 拒绝启动，不放在命令行中。`EXECD_ENVS` 固定在 root 控制区，普通文件工具不可写。最终 Kubernetes 安装必须保留同样的启动环境保护，不能因为使用原生 argv 就假设继承环境已清空。

所有提供的文件方法均绑定官方 `files_with_identity(10000,10000)`，owner/group 固定 `bbx-solver`，特殊 mode 位拒绝。接口不支持或低权限访问失败直接报错，未提供 root fallback。文件通过内核身份约束，不做自建字符串路径 ACL；同 Task 文件共享，不承诺逐 Agent 文件或进程隔离。

本机 SQLite 仅保存 P1 的不可变 admission、SDK 输出文本与观察事件，不写另一套可变执行状态。P3 必须接回现有 ToolInvocation/调用账本、平台 fencing、输出归档和生命周期，不能把它发布成第二套权威。前台流使用 SDK `skip_accumulation`；后台保留权威 source cursor，按来源区间排序重建预览、去重并允许补回前缀。source 区间长度不等于解码文本重编码长度时仍保存观察；替换字符、无法精确裁切的文本区间及未收集区间独立标记，不能把重编码长度当原始输出长度。

预览有界；`byte_count_basis` 区分后台服务端 cursor 与前台 SDK 文本 UTF-8 字节数。`text_integrity`、`source_gaps`、`text_gaps` 说明覆盖，`raw_bytes_retained=false` 明确没有原始命令输出字节保全保证。`retained` 只表示已观察文本的相应覆盖，不表示原始二进制完整；未拉取/过期的上游日志也不保证已归档。二进制文件仍使用官方字节文件接口。

## 网络与归属证明范围

测试容器内创建两个 Worker、一个 helper 和一个本地目标的独立 netns/veth。可信夹具配置根 INPUT/FORWARD；采集用旧镜像已有 tcpdump/tshark，代理用原有 mitmproxy 12.2.3。NET_RAW 档还需要按实际 veth 固定源地址，防止伪造包被错误归属。没有启用官方 Egress 或传入 OpenSandbox networkPolicy。

窄代理扩展验证每命令口令、实际来源地址、Assignment/generation/epoch 与网络身份；发布使用本机文件锁。先拒绝实际控制 IP、loopback、unspecified、link-local，再检查获准数值目标范围；IPv4-mapped IPv6 先归一化，HTTP 重定向逐请求重检。实际 control_addresses 必须来自可信网络装配，不能由模型提供。

当前只证明数值 IP HTTP 路径。主机名按 fail-closed 拒绝；完整 DNS/TLS/CONNECT、IPv6 正常业务、MTU、UDP/回连、Runner 单 VPN 与可信 Lead 提交、Cilium、跨任务和 Kubernetes 生命周期还未验证。未接进程 eBPF，进程观察为 `disabled`；本包的命令登记与 PGID 检查不代表所有解释器语句/脱离进程组后代审计。

## 复现

从仓库根目录进入本目录，使用独立环境。不要执行 `make up`，不要依赖 `.env`。

```sh
cd services/sandbox-execution
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --frozen --python 3.12
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration
```

构建不访问网络，复用已拉取镜像；缺少镜像时先单独核对并拉取上述 digest。集成测试用 testcontainers、随机 loopback 主机端口、`bbx-archv2-` 名称，自动清理容器。控制进程仅获显式 `SYS_ADMIN/NET_ADMIN` 例外（Docker 默认另含降权所需能力与 NET_RAW），未启用 privileged；Kubernetes/其他运行时的能力和 seccomp/AppArmor 组合仍是 G1 发布门禁。测试不连接真实模型、靶场、VPN 或现有平台。

审阅后可清理本轮镜像；原始镜像与现有平台保持不动：

```sh
docker image rm bbx-archv2-sandbox-seam-test:20261106 bbx-archv2-sandbox-seam:20261106
```

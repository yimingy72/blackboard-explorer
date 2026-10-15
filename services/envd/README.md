# envd 执行环境

## 任务流量与 Agent 网络

新镜像支持 `ENVD_TRAFFIC_ENABLED=true`，并要求固定的 `ENVD_TASK_ID`。runtime 需增加
`NET_ADMIN`、`SYS_ADMIN`，设置 `net.ipv4.ip_forward=1`、
`net.ipv6.conf.all.forwarding=1` 并保留 `/dev/net/tun`。envd 用新增能力建立每 Agent 的
netns/veth；执行命令前通过 `setpriv --bounding-set=-sys_admin` 移除 Agent 进程及 sudo
后代的新增 SYS_ADMIN，保留既有 NET_ADMIN/NET_RAW。文件系统、安装目录和工作区仍共享。

每个 Agent 的 localhost、路由、VPN/TUN 独立。`BBX_AGENT_IP`、`BBX_AGENT_IPV6`、
`BBX_TASK_GATEWAY` 与 `/workspace/shared/network.json` 提供网络地址；跨 Agent 服务应监听
`0.0.0.0` 并使用对方地址。任务网关为 `198.18.0.1/24`，IPv6 使用任务专属 ULA；已有路由
冲突、命名空间创建或 DNS 失败时明确拒绝命令，不回退到共享 root 网络。外部 IPv6 连通仍取决于
部署网络的 IPv6 路由。dnsmasq 转发原 Docker resolver，iptables/ip6tables 提供任务出口 NAT。

root 网关和各 Agent netns 各使用一个官方 tshark（及其 dumpcap 子进程），同一采集源只有一个
AF_PACKET socket，`-i any -s 0 -y LINUX_SLL2` 覆盖 TCP、UDP、ICMP、DNS、loopback 及后来
创建的 TUN；原始 PCAPNG 按真实采集源归档。Agent 源为 exact，网关源为 shared，
不根据 UID 或时间猜归属。根网关片仅过滤经认证的 start 请求实际 peer 与本地 envd:8080 的
双向 TCP 控制面连接；Agent netns 不使用该过滤，其他目标的 8080 端口仍抓取。
10 秒或 16 MB 关闭原始分片，空片不归档，不覆盖旧片；同时用
`-l -P -V -T ek -x` 逐包输出原生协议字段和 frame bytes，元数据每 0.5 秒独立封存，
不等待原始文件关闭才展示。`record_kind=packet` 的 source 包号单调，native frame 包号
随原始分片重置；capinfos 的真实包数建立封存片范围。Info 来自原生列，SLL2 的方向与
ifindex 来自抓包头，实际接口名由对应 namespace 查询，lo 明确标 loopback。
`protocol/transport_protocol` 根据 tshark 的传输层字段稳定分类 TCP/UDP/ICMP；
`application_protocol` 另存归一化应用协议。
随机端口触发应用层猜测时仍可按真实 UDP/TCP 检索；解码 HTTP/HTTPS 保留原分类。
启动必须收到 tshark 的实际文件就绪通知才允许 Agent 命令。终态及限额封存前保留
300 ms 尾批交付窗口，覆盖 dumpcap/libpcap 的 250 ms 读取批次，避免立即中断时遗漏
刚完成命令的最后一批包；此窗口不用于猜测 Agent 归属。
原始包及私有 hex 保留原始数据；协议树和文本展示脱敏。每条详情最多 512 节点、8 层、
64 KiB 协议树和 16 KiB 原始字节，未知字节偏移保持 null，不猜测。截断均带明确标记。
EK 单行上限 2 MiB，预览队列最多 1024 条且总计不超过 16 MiB；满时继续读取并计入
`preview_dropped`，与内核 `dropped_packets` 分开，原包仍保存。消费最多每 32 条让出
事件循环；元数据批最多 128 条且 900 KiB，避免逐包对象和原始数据重复归档。
封片前补齐冻结 DTO 的节点、记录及 observation 默认字段，再按完整 UTF-8 JSON 字节计量，
防止归档端模型展开后超过 1 MiB；测试同时校验完整共享模型及最长全局记录编号的大小。

单个 mitmdump 12.2.3 在任务网关监听。每条命令注册随机代理认证，子命令和 sudo 继承代理与
任务 CA；代理认证由官方组件去除，不发送给目标。CA 私钥和代理身份留在 root 私有目录，
不会以控制文件形式进入工作区归档；短期代理认证跳上的原始字节可能存在于私有原始 PCAP，不能把原始包称为已脱敏。公共 CA bundle 和系统信任用于 curl、Python/requests、Git、Node 等常见
客户端；不降低上游 TLS 校验。流式请求/响应通过官方 stream 回调透传，仅保留每侧 16 KiB
展示预览。证书固定、自有 TLS 信任库、QUIC、绕过代理及 Agent 自己 localhost 的直连 TLS
保留密文，不声称已解码。普通命令结果及审计导出会隐藏临时代理密码。

内部接口均沿用任务 Bearer token：

| 方法 | 路径 | 内容 |
|---|---|---|
| POST | `/traffic/start` | `{task_id,run_number}`，同一任务/轮次幂等；返回稳定 `source_id` |
| GET | `/traffic/status` | 状态、HTTPS 能力、累计原始字节、丢包及不完整原因 |
| GET | `/traffic/records` | `after_sequence`、`limit`、`agent_id`，仅返回摘要 |
| GET | `/traffic/records/{id}` | 有界脱敏详情 |
| POST | `/traffic/seal` | 即时封存当前逐包/HTTP JSONL；不等待原始包定时关闭，采集继续 |
| GET | `/traffic/chunks` | `{source_id,items,status}`，每片最多 128 条、900 KiB detail |
| GET | `/traffic/chunks/{id}` | 不可变 JSONL 流 |
| GET | `/traffic/artifacts` | 已封存原始片，包含真实 `agent_id/source_id`、大小、校验和 |
| GET | `/traffic/artifacts/{id}` | 不可变原始 PCAPNG 流，仅接受受控 ID |
| POST | `/traffic/ack` | `{chunks:[],artifacts:[]}`；runtime 确认 MinIO 持久化后幂等删除本地片 |
| POST | `/traffic/limit` | 停止持久化并封存，明确 partial；保留代理透传及后续 Agent 命令 |
| POST | `/traffic/stop` | 终态关闭代理和所有抓包进程，封存最后分片 |

`ENVD_TRAFFIC_MAX_BYTES` 默认累计原始包 2 GiB，并限制未确认本地文件占用；
`ENVD_TRAFFIC_MAX_RECORDS` 默认 1,000,000。容量、丢包、工具失败或重启中断均明确 partial。
记录预算为 HTTP 解码预留最多 10,000 条，避免原始网络摘要先耗尽所有展示记录。
达到容量后保留网络转发，不再生成记录或文件；任务结束时仍需 `/traffic/stop`。
数据在 `/var/lib/bbx/traffic`，由 runtime 上传 MinIO；envd 不持有对象存储凭据。
重启保留已封存片、恢复未索引 JSONL 和原始片的有界快照；原网络进程状态未知时拒绝接管命名空间，
应由 runtime 先归档可恢复数据，再替换容器。旧镜像没有 health `traffic` 能力标记，不补造历史。

合成验证（不调用模型或真实目标）：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pytest services/envd/tests -m 'not integration'
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pytest services/envd/tests/test_traffic_integration.py -m integration
```

第二条要求事先构建 `bbx-traffic-envd:latest`，或用 `BBX_TRAFFIC_TEST_IMAGE` 指定同版镜像；
测试创建随机网络和 `bbx-traffic-*` 临时容器并清理，覆盖 HTTPS/流式/sudo、双 Agent 的
TCP/UDP/ICMP/IPv6/TUN 归属、DNS/NAT、原始片下载及容量限制后继续转发。
`test_live_packets_integration.py` 另证明持久命令尚未结束时孤立单包已可见、原片尚未封存，
并在既有 4 GiB/2 CPU 配置测两 Agent 正常/突发的延迟与 RSS；报告写到忽略目录
`.data/traffic/live-packet-metrics.json`。

原生分析使用同一个官方采集 socket 写出的 PCAPNG，`status.native_enabled=true` 表示本轮支持：

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/traffic/native` | 完整 source index 前缀及待归档 `pending` 分片；每项带 kind、source_id、descriptor |
| GET | `/traffic/native/parts/{id}` | 内部私有字节流，包含 PCAPNG 或 keylog 分片 |
| POST | `/traffic/native/ack` | `{"parts":["n-..."]}`；幂等删除已归档暂存字节，保留完整历史索引 |

每 0.5 秒及 seal 时发布完整 PCAPNG 块，常规分片不超过 1 MiB，单块硬上限 4 MiB；
只检查 SHB 字节序、块长度、对齐及尾长，不解析网络协议。原始字节不改写，跨轮转片按同一
source 连续编号；每个 segment 的 offset 从 0 开始。旧原片 ACK 前先发布 native 尾部。
重启恢复完整前缀并明确结束旧 epoch，不能冒充连续采集；只有旧原片、元数据及 native 暂存均
确认后才允许开启下一轮。暂存最多 4096 片且受任务容量限制，每 source 历史最多 65536 片，
达到限制显示 incomplete/error；不会丢弃未确认原片而假报成功。

`MITMPROXY_SSLKEYLOGFILE` 只注入 mitmdump，预建文件和分片均为 0600。官方 keylog 回调
按完整行写入，最大 16 MiB；达到容量或 `/traffic/limit` 后停止写 secrets，继续代理转发。
keylog 只通过内部 native 归档链路传输，不进入 Agent 环境、普通 PCAP 文件清单或命令日志；
CA 私钥仍不归档。TShark 用该文件和默认重组偏好解码实时原生 HTTP，历史事务记录保持兼容。
未支持 native 的旧镜像不返回 capability，不能推断其历史 TLS 可解密。

`bbx-exec-env:latest` 在容器内以 root 运行 envd，监听 8080。每任务传入独立的 `ENVD_TOKEN`；`/mcp` 和内部接口均需 `Authorization: Bearer <ENVD_TOKEN>`。`/mcp` 的 `execute_command` 还需 `X-Agent-Id: agent-1` 一类请求头，用户须先通过 `/users` 创建。

token 约定：agent-runtime 持有 `ENVD_TOKEN_SECRET`，以任务 UUID 的标准小写连字符字符串为消息，计算 HMAC-SHA256 的十六进制摘要作为 token，并在创建容器时以环境变量 `ENVD_TOKEN` 注入。envd 只读取 `ENVD_TOKEN`，不接收 `ENVD_TOKEN_SECRET`；命令子进程会移除这两个环境变量。默认直连模式不注入代理变量；显式代理隔离模式才保留代理配置。派生逻辑由 agent-runtime 实现。

## 构建

在仓库根目录构建执行环境镜像；只有使用代理隔离模式时才额外构建可选代理镜像：

```sh
make image-exec-env
# 显式代理隔离模式另运行：make image-egress-proxy
```

| Make 变量 / Docker 构建参数 | Make 默认值 | 用途 |
|---|---|---|
| `APT_MIRROR` | `http://mirrors.aliyun.com/ubuntu` | 替换 Ubuntu 主仓库与安全更新仓库的地址；值为不带末尾 `/` 的仓库 URL。 |
| `PIP_INDEX_URL` | `https://mirrors.aliyun.com/pypi/simple` | envd 的 pip 安装使用此索引 URL。 |
| `APK_MIRROR` | `mirrors.aliyun.com` | 替换 Alpine 仓库域名；值为不带协议和路径的主机名。 |

Make 将这些变量通过 `--build-arg` 传给对应的 Dockerfile；可以在命令行覆盖任意一项。将三项显式设为空即可使用官方源：

```sh
make image-exec-env APT_MIRROR= PIP_INDEX_URL=
# 可选代理镜像：make image-egress-proxy APK_MIRROR=
```

两个 Dockerfile 的参数默认均为空，直接运行 `docker build` 时默认保留官方源。这些参数只用于镜像构建，不配置容器运行时的出网代理。

若 Docker 构建直连镜像站超时，而宿主机通过本地 `7897` 代理可访问，可显式传入构建代理。BuildKit 中若无法解析 `host.docker.internal`，同时加上主机网关映射：

```sh
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
make image-exec-env
# 可选代理模式还需：make image-egress-proxy
make test-integration
```

`DOCKER_BUILD_ARGS` 默认为空，用于向镜像构建目标传入额外 Docker 构建参数；构建和集成测试使用相同值可复用缓存。它不配置容器运行时环境。

镜像构建需要 Docker 可用。`make test-integration` 会按 Makefile 构建集成测试需要的执行环境、代理、运行器与评测环境镜像，即使日常部署默认直连；如需切换软件源，应同样传入对应变量。仓库 `.dockerignore` 排除真实 `.env`、Git 元数据与本地虚拟环境，避免它们进入构建上下文。

## 接口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok","runtime_audit":true}` |
| GET | `/audit` | 无 | `{"format":"bbx.runtime-audit.v1","commands":"<JSONL>","sudo":"<text>"}`；日志缺失 503、合计超过 32 MiB 返回 413 |
| POST | `/users` | `{"agent_id":"agent-1"}` | `{"agent_id", "home"}`，幂等 |
| GET | `/files?path=` | `/workspace` 下绝对路径 | 文件字节；超限 413 |
| GET | `/stat?path=` | `/workspace` 下绝对路径 | `exists`、存在时的 `size`、`is_file`、`is_dir` |
| POST | `/archive` | 无 | `application/zstd` 的 tar.zst；`X-Archive-Fallback: none` 或 `agents-only` |
| POST/GET/DELETE | `/mcp` | MCP streamable HTTP | 仅 `execute_command(command, cwd, timeout_sec, privileged)` 工具 |

`execute_command` 返回 `exit_code`、`stdout`、`stderr`、`truncated`、`full_output_path` 和 `execution`（命令 ID、Agent、目录、起止时间、耗时、状态、提权请求标记）。stdout/stderr 以 `<command_output>` 标签包裹。默认工作目录是 `/workspace/agents/<id>`。输出总量超过 64 KiB 时显示头尾各 32 KiB，完整输出写入该 Agent 的 `.outputs/`。超时的退出码为 124。普通命令以 Agent 用户运行；`privileged=true` 由该用户通过 `sudo -n` 执行任意 shell 命令。Agent 也可在普通命令中自行调用 `sudo -n`。

envd 将每次命令的开始与结果写入 root 持有的 `/var/log/bbx/commands.jsonl`；sudo 将实际提权调用和退出状态写入 `/var/log/bbx/sudo.log`。两个文件在服务启动时建立，`/audit` 返回其文本快照。若文件后来被 root 删除，接口返回 503，不能视作完整审计。已获 root 的进程可篡改容器内日志；sudo 对 shell 或脚本记录一次调用及退出，不会逐条记录内部子命令。归档前由 runtime 持久化快照，`execution` 随已有工具记录持久化。

配置环境变量：`ENVD_TOKEN`（必填）、`COMMAND_TIMEOUT_MAX`（默认 1200 秒）、`EVIDENCE_MAX_BYTES`（默认 50 MiB）、`ARCHIVE_MAX_BYTES`（默认 2 GiB）、`ARCHIVE_EXCLUDE`（逗号分隔）。旧 `PRIVILEGED_PREFIXES` 仅保留配置读取兼容，不再限制执行。

## 隔离运行

agent-runtime 创建容器时使用可写根文件系统，保留 Docker 默认 capabilities、增加 `NET_ADMIN`，挂载 `/dev/net/tun`，并按 profile 设置 CPU、内存、PID 上限。执行容器不设置 `no-new-privileges`；不使用 Docker privileged 模式、宿主网络或 Docker socket。默认执行容器只接入有网关的 exec 网络，不设置代理。下例为**显式代理隔离模式**：exec 网络改为 internal，HTTP(S) 代理变量指向同时接入 exec 与有网关网络的 egress-proxy。

以下示例可直接运行；使用示例 token，请在实际任务中替换。结束后执行末尾清理命令。

```sh
make image-exec-env image-egress-proxy
docker network create --internal bbx-m2env-exec
docker run -d --name bbx-m2env-proxy --network bbx-m2env-exec --network-alias egress-proxy -e EGRESS_ALLOWLIST=example.com bbx-egress-proxy:latest
docker network connect bridge bbx-m2env-proxy
docker run -d --name bbx-m2env-envd --network bbx-m2env-exec --cap-add NET_ADMIN --device /dev/net/tun --cpus 2 --memory 4g --pids-limit 256 -e ENVD_TOKEN=replace-me -e HTTP_PROXY=http://egress-proxy:8888 -e HTTPS_PROXY=http://egress-proxy:8888 -e http_proxy=http://egress-proxy:8888 -e https_proxy=http://egress-proxy:8888 bbx-exec-env:latest
docker exec bbx-m2env-envd curl -fsS -H 'Authorization: Bearer replace-me' http://127.0.0.1:8080/health
docker rm -f bbx-m2env-envd bbx-m2env-proxy
docker network rm bbx-m2env-exec
```

# 任务流量采集与逐包查看：交付报告

日期：2026-10-15。工作树：`~/bbx-wt/traffic-observability`，分支：`feature/traffic-observability`。

**当前交付为文末 V3 补充报告。** 下方原报告保留 V2 验证记录；其中分标签页、关键词筛选、EK 字段树的限制已由 V3 替代，镜像与检查结果以 V3 为准。

## 完成内容

1. 普通黑板、CTF 团队共用任务级采集及 MinIO 归档。新任务/续跑新轮开始采集；旧任务不伪造历史包。任务结束后可读取，删除任务时清理各轮流量对象。
2. 同一执行容器内按 Agent 建立网络命名空间、veth 和独立采集源，以来源确认 TCP、UDP、ICMP、IPv6、localhost、动态 TUN 流量。共享网关单列 `shared`；身份未知单列 `unknown`。CTF 保留 `lead` / `member-N` 的业务身份，不从 Linux 用户名反猜。
3. HTTPS 使用官方 mitmproxy 和任务专用 CA；常见命令、sudo 命令继承代理/CA，保留上游证书校验。HTTP/HTTPS 可读内容、MAF 模型/MCP/工具逻辑调用与原始包区分展示。
4. 单采集源仅一个抓包 socket，由官方 TShark/dumpcap 同时保存轮转 PCAPNG 和持续输出 EK 逐包数据；不等待命令结束或分片封存。提供原生 Info、收发方向、协议字段树、有限原始字节预览。源包号独立递增，原生帧号允许随分片重置。
5. 采集元数据与 PCAP 独立上传，原始大文件上传不持有元数据发布锁。逐包详情批量保存：每批最多 128 条/1 MiB，避免每个包一个对象；每次归档最多 8 批/8 MiB，确认清单发布后 ACK，重启按来源检查点去重恢复。
6. API 增加 `kind/direction/after` 筛选；增量按已发布清单的游标读取，空匹配也可推进，不能越过未发布数据。包详情、原始包只对有权用户/服务开放，Agent token 拒绝。缺失/损坏/超限对象返回 503，不充当空列表。详情与下载均 `Cache-Control: no-store`。
7. 前端默认逐包工作区，复用 antd Table 虚拟列表、Splitter、Tree、Drawer、Tabs、Select。每行显示编号、时间、Agent、方向、端点、协议、长度和 Info；下方可拉伸协议树和十六进制/ASCII 窗口。协议字段具有真实字节范围时可联动高亮，没有范围时不猜。
8. 支持轮次/Agent/协议/方向/地址端口与摘要关键词筛选、每秒增量更新、自动跟随、暂停画面及恢复追赶、历史翻页。浏览器只保留最近 500 包的显示窗口和有界待显示队列，存档不受此显示窗口限制。
9. “HTTP / 调用”另读解码请求响应和模型/工具追踪，保留密文标识、命令关联、逻辑调用与 SDK 重试未知语义。任务入口及 Agent 会话入口共用面板，打开流量不卸载原会话/草稿。
10. 修复验证中发现的采集尾批丢失、启动就绪竞争、随机 UDP 端口被应用启发式误判、恢复来源 ID 漂移、clean suspend 遗漏逻辑观察等问题。前端复核还修复中途读取失败跳过增量、空态转采集后尺寸监听失效、ICMPv6 归一筛选错配；保留严格真实数据断言，没有通过弱化测试掩盖问题。

## 实际验证

最终检查针对 V2 源码，不把 V1 检查数当成 V2 通过：

| 命令/检查 | 实际结果 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check` | **2094 passed，348 deselected**；ruff format/check、pyright 全绿；61.17 秒 |
| `make test-integration -o image-exec-env -o image-egress-proxy -o image-agent-runtime -o image-eval-env` | **344 passed，2098 deselected**；587.90 秒；这些镜像已从本次代码构建，避免重复安装依赖 |
| `pnpm --dir web check` | ESLint、TypeScript 全绿；**125 tests passed / 22 files** |
| `pnpm --dir web build` | 成功；仍有仓库既有大 chunk 提示，未提高阈值掩盖 |
| antd CLI lint：PacketInspector / TrafficDrawer | 各 **0 issues**；使用公开 API/token，无 `.ant-*` 内部样式覆盖 |
| 两模式真实采集→MinIO→黑板查询集成 | 独立执行 **2 passed / 54.52 秒**，也在整仓集成范围内；不是额外叠加的通过总数 |
| 真实 Ubuntu 综合检查 | **2 passed / 116.83 秒**；Kali 同代码综合通过 |
| 浏览器实际交互 | 前一轮 17 项 + 逐包版 11 项人工驱动检查通过；合成页面，不调用模型/真实靶场 |

强实时集成断言：第一 Agent 注册后持续发送 10 秒 UDP，4 秒内 API 的 `kind=packet&after=` 已出现对应包，协议树与 hex 非空；此时命令尚未完成、该 Agent 第一份 10 秒 PCAP 还未封存。同时保留双 Agent 的 HTTP/HTTPS/TCP/UDP/ICMP、两轮记录、ACK 恢复、容器销毁后的 Agent 筛选及原包下载断言。

浏览器逐包版覆盖：暂停期间后台继续收集并计数、恢复追赶；协议值及 byte-range 高亮；历史翻页；CTF canonical Agent 筛选；UDP 含 DNS 的传输层筛选；发送方向；500 包窗口和实际约 11 行虚拟 DOM；独立 HTTPS 正文；历史空态；空态转实时后的 ResizeObserver 重新绑定；390px 无页面横向溢出、输入字体 16px、键盘选包；503 不显示成“没有流量”。

离线增量测试专门模拟“第一批成功、第二批 503”，确认游标和记录一起提交，失败后从原游标重试、不重复也不漏过成功批次。

### 实测负载

两 Agent、4 GiB / 2 CPU 的执行资源配置不变。最终优化使用 1024 条且总共不超过 16 MiB 的预览队列、每 32 条主动让出、复用写入 fd 及已编码批次；不增加无界队列、不重复抓包。

- 孤立数据包在命令未结束、源 PCAP 未封存时可见：Kali 约 **294 ms**，Ubuntu 约 **80 ms**。这是采集侧观测；前端还有默认 1 秒轮询、归档及网络延迟，不承诺端到端固定毫秒数。
- 正常 200 包后，两轮各 3000 包的本地突发场景：最终约 6200 条逐包预览，**预览丢弃 0、原始丢包 0**。
- cgroup 内存：Kali 约 345→424 MB，Ubuntu 约 287→365 MB，跨 PCAP 轮转后仍稳定。进程 RSS 求和包含共享页，不作为实际容器内存。
- 极端无间隔突发期间 MCP 命令往返约 0.85–1.15 秒，命令均完成。更高负载仍可能丢弃预览；该路径保留独立计数和 `partial` 状态，不声称无限吞吐。

检查日志、指标、截图和源码/镜像 hash 在本工作树 `.data/traffic/`；不入 Git，不含生产凭据。UI 预览中的数据及原包均为合成材料。

## 本地环境

已重建并替换两个无状态应用容器，更新 Ubuntu/Kali 执行镜像。测试地址：**http://127.0.0.1:58000/**。

仅从现有 Docker 容器配置在内存中继承环境变量、端口、网络、别名和挂载，不读取/输出 `.env`，不另存生产凭据。Postgres/MinIO 未重建、未清理数据卷。替换前无运行中任务；替换后任务数 17、Session 数 99，任务标识/状态及 29 份 Worker profile、5 份平台配置、应用设置的数据库摘要一致。认证 API 和新增流量 API 实际读回正常；旧任务返回 `not_recorded`。

| 镜像 | 本地 ID 前缀 |
| --- | --- |
| `bbx-ctf-blackboard:latest` / `bbx-blackboard:latest` | `bae851f1081e` |
| `bbx-agent-runtime:latest` | `6ba9ba421199` |
| `bbx-exec-env-kali:latest` | `c34980f6a5f3` |
| `bbx-exec-env:latest` | `c86b42b3a869` |

复核了安装模块及前端产物 hash 与工作树一致。自己的测试容器/网络及更新时的旧应用备份容器已清理；未停止用户其他服务和靶场。

无需用户补跑构建命令。刷新平台后，新建任务或续跑的新轮可测试真实流量；旧轮无法补录。本轮没有发起真实模型任务或访问用户靶场，没有评估费用。

## 兼容边界与偏差

- **网络共享语义变更**：文件、软件安装仍共享；每个 Agent 的 localhost、端口、路由、VPN 独立。跨 Agent 服务使用 `BBX_AGENT_IP`、`BBX_AGENT_IPV6`、`BBX_TASK_GATEWAY` 和 `/workspace/shared/network.json`；不要把他人的 localhost 当作自己的。只有 envd 使用新增 SYS_ADMIN 管理命名空间，执行 Agent 先降去该能力，原有 sudo/NET_ADMIN/NET_RAW 范围保留。
- 证书固定、独立信任库、绕过代理和 QUIC 等可能只有密文。协议树不是对 TLS 正文的猜测；原始包和原始 hex 是私有、未脱敏的数据，可含短期代理认证；树及可读正文为有界脱敏副本，CA 私钥不归档。
- MAF 组件复用于模型/工具逻辑追踪及上下文归属。其 SDK 内部重试的独立 wire 次数未知，不把一次逻辑调用冒充多条完整网络包；执行容器通信才是内核逐包采集。
- 关键词筛选不是完整 Wireshark display-filter 语言；协议树来自官方 EK 字段，未提供真实 offset 的字段不会高亮字节。原包仍可交给 Wireshark 深入分析。
- `TRAFFIC_MAX_BYTES=2147483648` 是本轮原始包限额，不是全部详情对象总存储配额；`TRAFFIC_POLL_SECONDS=1`。原始丢包、预览丢弃、归档积压分别展示；达到容量上限封存采集，代理转发和任务命令继续。
- 构建中曾因显式 build 代理的宿主映射解析失败而失败，后续正常镜像构建成功；未改 Docker 全局设置或绕过限制。较早的集成失败已定位并修复，最终整仓结果如上。

## 建议提交划分

本轮未合并 main、未推送。设计已按项目规则先单独提交：`80349ee`、`0a5d88e`、`bd70d7b`。

1. `style(web): polish task lists and page transitions`：此前已交付的首页/列表/设置/骨架过渡基线，原 `ui-rebuild` 工作树保持不动；基线路径及 hash 见 `.data/traffic/frontend-baseline.json`，与流量入口重叠的文件按 hunk 拆分。
2. `feat(traffic): capture attributed task packets and decoded HTTPS`：contracts/schema/OpenAPI、envd、runtime、blackboard API/删除清理、镜像配方、SDK 依赖和 lock，以及相应后端测试。
3. `feat(web): inspect live packets by agent`：逐包面板、HTTP/调用面板、两个工作台/Agent 入口、API client/types、增量及 hex 测试、使用文档与本报告。

下一步由用户在新任务测试流量交互；现有 HTTPS 解码限制、原包与展示副本差异需保留，后续优化以真实任务流量和存储/延迟测量为依据。

## V3 补充：统一列表与 Wireshark 原生分析

用户本次要求数据包、HTTP 调用放在一起，过滤规则与 Wireshark 一致，并重做捕获及详情界面。设计先独立提交 `7298a6f`，正本见《黑板系统实现架构》9.4；本节替代 V2 的标签页与简化筛选说明。

### 完成了什么

1. 移除“数据包 / HTTP 调用”标签页，按时间统一显示真实数据包、解码事务、MAF 逻辑调用。包号沿用采集源帧号，事务/逻辑记录使用 `R` 前缀和来源图标，保留 Agent 归属，避免把一次 HTTP 事务误计为额外 wire 包。
2. 使用官方 **sharkd / Wireshark 4.4.19** 原样校验及执行显示过滤器，不自研过滤 DSL。例如 `tcp.port == 443`、`ip.addr == 192.0.2.1 && tcp.flags.syn == 1`、`http.response.code == 200`。字段可直接应用其原生过滤表达式，语法错误与基础设施错误分别显示。
3. 完整原生协议树、真实字段标签、生成字段、专家级别与 data source-aware offset/length；Frame、Decrypted TLS、重组数据源可切换，字段联动 hex/ASCII。切到不属于该字段的数据源时保留选择并取消高亮，避免错误映射。HTTP 事务按请求/响应格式化展示头与正文，逻辑调用显示追踪属性，不伪造协议树。
4. UI 统一明亮蓝色、紧凑工具栏与协议弱底色；公开 antd Table 虚拟列表、Splitter、Tree、Select、Drawer，协议树 16px 缩进和单行横向滚动；桌面协议/字节并列、窄屏纵向，输入字体 16px，暂停、跟随、历史、轮次及 Agent 范围保留。没有引入新前端依赖，没有 `.ant-*` 内部样式覆盖。
5. envd 将 PCAPNG 按完整 block 边界导出不可变字节片；mitmdump 通过官方 SSL keylog 接口写入私有完整行片。runtime 独立上传字节片/密钥片、哈希校验、发布来源索引和 manifest 后 ACK；保留跨分片重组上下文、重启前缀恢复及终态排空，归档成功后才销毁环境。
6. 每个 Agent 采集源独立解析，避免不同 netns 的相同 localhost 五元组或帧号串流。原生结果带 revision 和覆盖范围；后续数据/密钥能改变老包的重组与匹配，界面重新读取快照，不只追加。旧 revision 409 重新跟随，查询淘汰 404 自动重建，服务错误不显示成空匹配。
7. 原生分析有界：最多 2 个工作进程、8 个查询，输入缓存 2 GiB、总磁盘 4 GiB，单工作进程资源/时间受限，结果最多 50,000 包且明确标记截断。频繁快照更新合并，最短约 4 秒重建间隔，并按实际构建耗时延长；不对每次前端轮询全量扫描 2 GiB。
8. 分析进程低权限、空 HOME/配置、去除服务凭据、no-new-privs 与禁止联网。仅提供固定 check/frames/frame 包装，不暴露任意 sharkd RPC/路径。任务删除清理工作进程、输入/密钥及缓存；应用崩溃残留目录按 PID 出生信息回收。
9. 审核修复真实 sharkd `l/f/h/ds` 形状下敏感字段标签/过滤表达式泄漏、检查器累计 32 条过滤器后耗尽、旧来源索引随轮询无限增长等问题。旧索引保留 180 秒供读者过渡后回收，当前及终态索引保留；解析输入在后台线程验证，避免 32 MiB 索引阻塞事件循环。
10. 100 KB 跨分片合成 HTTPS 验证发现默认字段展开使预览 bundle 超过 1 MiB。生产侧按规范展开后的大小限制节点，传输侧省略 DTO 默认值且恢复内容一致；保留 1 MiB 限额，没有通过放大限额或默默丢数据解决。

### 实际运行的最终检查

| 检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check` | **2129 passed，349 deselected，58.19 秒**；ruff format/lint、pyright 全绿 |
| `BBX_NATIVE_TEST_IMAGE=bbx-traffic-native-blackboard:test UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration -o image-exec-env -o image-egress-proxy -o image-agent-runtime -o image-eval-env` | **345 passed，2133 deselected，617.08 秒**；依赖镜像均提前由本次代码构建，跳过重复构建目标 |
| `pnpm --dir web check` | ESLint、TypeScript 全绿；**129 passed / 22 files** |
| `pnpm --dir web build` | 成功；既有 chunk 体积提示保留 |
| antd CLI lint：PacketInspector / TrafficPacketDetails / TrafficMessages / TrafficDrawer | 各 **0 issues，partial=false** |
| Kali / Ubuntu 最终真实采集验证 | Kali **2 passed / 97.68 秒**；Ubuntu keylog 场景 **1 passed / 29.59 秒** |
| 两模式真实 envd→runtime→MinIO→API | **2 passed / 93.07 秒**，与上方整仓范围重叠，不额外累加通过数 |
| 浏览器驱动生产前端、真实官方 sharkd | **12 项通过**；过滤结果与原生字节来自真实合成 PCAP/TLS/keylog，未用前端假匹配器 |
| 本地已部署服务 | 原生校验 `tcp.port == 443` → **200 / valid=true / 4.4.19**；错误 `http &&` → **422**；正式首页 index hash 与本次 dist 完全一致 |

浏览器覆盖统一列表/HTTP 格式化、真实 HTTP 字段过滤、原生错误语法、Agent 与复合过滤、SYN/ACK 标志位、真实 LRU 查询淘汰后自动恢复、清除过滤、数据源切换不交叉高亮、390px 无页面横溢/16px 输入/真实字段字节映射。截图：`.data/traffic/native-unified-desktop.png`、`native-unified-mobile.png`；逐项记录 `browser-native-checks.json`。原生跨片 TLS 测试验证 100 KB 内容、源上下文保持、已发布字节与原包一致、未封存时输入已可用、停止/重启恢复。检查与构建日志同在 `.data/traffic/`，不入 Git。

全部使用本地合成服务、包与脚本化模型，无真实模型调用、无靶场访问、无评估费用。不读取或修改 `.env`。

### 本地环境与用户测试

测试地址仍是 **http://127.0.0.1:58000/**。V3 后端/runtime 已在无运行中任务时替换；最后的前端细节通过仅复制静态产物更新，无二次重启 runtime/blackboard，并保留旧 hash 资源供已打开页面使用。最终镜像已构建并更新正式标签，之后重建容器仍有本次代码。

| 镜像 | 当前 ID 前缀 |
| --- | --- |
| `bbx-ctf-blackboard:latest` / `bbx-blackboard:latest` | `cdf594fc1a79` |
| `bbx-agent-runtime:latest` | `7c7ee3ee1a5c` |
| `bbx-exec-env-kali:latest` | `c83fc64a98e6` |
| `bbx-exec-env:latest` | `1735df945b09` |

运行中 blackboard 基础镜像为 `fd4f1ca5d280`，其原生后端与最终版本相同，静态产物已更新为最终 `dist`；已核验官方引擎版本与前端 index hash。部署记录见 `local-deployment-v3.json`、`local-web-v3-final.json`。

Postgres/MinIO 未重建或清理。更新前后任务 17、Session 99，任务标识/状态、29 份 Worker profile、5 份平台配置和应用设置的数据库摘要一致。自己的 native QA 容器、保活 fixture 和临时前端服务已清理；未操作用户其他服务/靶场。

无需用户补跑构建命令。刷新页面，使用新任务或续跑新轮验证采集；旧轮数据保留，但未收集的原生字节索引、TLS 解密材料无法追补。

### 偏差与待决

- 原生过滤语义以部署的 **Wireshark 4.4.19** 为准；不承诺与未来或其他版本字段完全相同。`frame.number`、`tcp.stream` 按采集源解释；同号跨 Agent 不构成同一帧/流。显示过滤器仅匹配真实包，MAF 逻辑调用不具备包字段，未过滤时仍统一展示。
- 当前为增量字节归档、快照分析；原生过滤/重组刷新有构建与归档延迟，页面明确覆盖范围。预览默认 1 秒读取不代表完整原生全量扫描每秒执行。达到结果/资源限额显示部分状态，不以无结果代替失败。
- 为保证重组及恢复同时保留原始 PCAPNG 与原生字节片，包数据存储约为原始两倍，另加详情/索引。尚未加入压缩合并或存储配额 UI，不为了这次界面需求引入存储层重构。
- 私有 keylog 与原始/解密字节属于未脱敏原始资料；可读树/请求响应为有界脱敏副本。证书固定、不经过代理及独立信任库等限制仍在，CA 私钥不归档。
- 该任务改动较大，是前序采集、归属、HTTPS、实时逐包和本次原生分析的连续交付。设计已依次先行提交；实现仍在任务分支，未合并 main、未推送。

### 建议提交划分

1. `style(web): polish task lists and page transitions`：此前首页基线，与流量入口重叠文件按 hunk 拆分。
2. `feat(traffic): capture attributed packets and archive native analysis inputs`：contracts/schema、envd netns/采集/原生字节与 keylog、runtime 上传/归档、镜像及关联测试/依赖。
3. `feat(traffic): analyze captures with isolated native Wireshark workers`：blackboard 分析缓存/受限子进程/API、鉴权清理、OpenAPI、原生真实引擎测试。
4. `feat(web): unify live traffic and native packet inspection`：统一 Drawer、原生查询/筛选、协议树与多数据源字节、前端测试及使用文档/本报告。

# 独立 CTF 比赛版设计、容量与工具核对报告

任务：`CTF-standalone-20261120`。分支：`codex/ctf-standalone-design-20261120`。本轮为用户要求的子代理协作、方案设计、接入准备和现有容器实测；独立程序尚未实现，线上平台没有切换。

## 1. 完成了什么

1. **取消死磕。** 三份设计正本与旧 Spec 的修订入口已同步：新比赛版允许 Lead 暂缓/恢复题目，Worker 提交理由与进展，协调器实际收尾后释放成员/环境。知识、会话及历史认领保留；全体暂缓不等于整场完成。
2. **确认团队与权限。** 初始 10 个同级 Worker；Lead 可读真实资源并请求增加团队人数。每题仍最多 10 人，每 Worker 最多持有 3 条 Intent。新增 Worker 由 Lead 请求并受本地实际准入控制，不恢复旧 helper，不将 3 个题目环境名额当全局 Worker 上限。
3. **自动援助。** 用户明确批准按就绪、未认领且可并行的方向自动增援，新题仍优先。先复用已有空闲成员，人员不足再向 Lead 提供缺口。3 条 Intent 是持有上限，不代表一个 Worker 有三个并发执行槽。
4. **形成最小独立版。** 一个 Node Pi 宿主、SQLite、本地工具和文件证据，另有轻量监督进程；移除比赛包对 Web、账号、PG、MinIO、Kubernetes、OpenSandbox、VPN/抓包和旧 helper 的依赖。现有完整平台保留。详细方案在[主方案](CTF-standalone-20261120.md)。
5. **核实官方接入。** 子代理读取[官方夺旗猫手册](https://cyber-arena.chaitin.cn/manual?tab=participant)，确定求解面为 `/openapi/v1/challenges`，不是账户 API 或求解 MCP。离线题也须 start；accepted 自动释放环境，rejected 不关闭环境；duplicate 不重试加分；`cumulative_score` 为整次 attempt 总分，不能按单题累加或被驳回的 0 清空。程序自行读本次帮助启动 Lead，普通异常不调用 finish。
6. **完成扩容保护设计。** Worker 准备、模型内存及工具启动共享准入预约；目标人数幂等，单批准备，压力暂停、低水位稳定后恢复。CPU 满载不直接判死。独立 watchdog 核实 PID/启动代次并确认旧执行结束后恢复，保留 deadline/会话/未决回执。见[资源保护](CTF-standalone-resource-guard-20261120.md)。
7. **实测持久 Pi 会话开销。** 在断网、4 核/6 GiB 临时容器中完成 1/8/16/32/64 会话的小历史样本，以及 16/64 会话的长历史样本，未调用模型或真实工具。
8. **核对工具与提示词。** 实际盘点当前镜像并用普通 Worker 身份运行离线夹具；对照群内 [Des-CTF-Knowledge](https://github.com/Dest1ny-Sec/Des-CTF-Knowledge) 的工具目录和脚本依赖形成缺口清单。最终镜像须生成 manifest，Worker 提示词使用分类短清单、准确解释器与路径，并可按需查阅完整用法。见[工具与提示词](CTF-standalone-tooling-20261120.md)。

## 2. 会话容量样本

镜像 `bbx-peer-runner:20261120`，实际 ID `sha256:d0cde061e246537a53411cdc30c2b93c0ecceeb9b22d90be0710bb4eb3d83657`，Node 22.21.1、Pi SDK 1.5.0。每组为独立新容器/进程；实际 cgroup 为 `cpu.max=400000 100000`、`memory.max=6442450944`，网络关闭。使用真实持久 SessionManager，并将合成消息同步到 Agent 上下文。

每轮包含 user、assistant toolCall、toolResult、assistant 四条消息；toolResult 是每会话独立的 8 KiB 随机文本，没有真正调用工具。小历史为每会话 16 轮/128 KiB 文本，长历史为 512 轮/4 MiB 文本。没有模型请求、压缩请求或 SQLite 工作负载。

| 会话数 | 每会话文本量 | 结束时 Node RSS | 结束时容器 memory.current | 集中追加期间事件循环最大延迟 |
| --- | --- | --- | --- | --- |
| 1 | 128 KiB | 134.6 MiB | 91.2 MiB | 3.24 ms |
| 8 | 128 KiB | 128.3 MiB | 86.0 MiB | 8.00 ms |
| 16 | 128 KiB | 136.2 MiB | 95.4 MiB | 14.34 ms |
| 32 | 128 KiB | 137.0 MiB | 99.0 MiB | 23.41 ms |
| 64 | 128 KiB | 139.5 MiB | 106.4 MiB | 28.23 ms |
| 16 | 4 MiB | 210.2 MiB | 246.5 MiB | 114.36 ms |
| 64 | 4 MiB | 630.2 MiB | 912.1 MiB | 208.14 ms |

这是每档一次的基线，不是统计容量上限，内存是结束快照而非全过程峰值。RSS 和 cgroup 用量有不同的共享页计量口径，不能互相替代。合成历史集中追加并回灌完整上下文，未模拟真实压缩和模型流式发送；不能把这张表称为“已支持 64 个真实解题 Worker”。会话创建计时从 SDK 导入后开始，不是容器/进程完整启动时长。

结果支持先用单宿主承载多个会话，后续应重点测长历史、模型在途、工具峰值、磁盘与事件循环的组合负载。不能将空会话平均内存直接除入 6 GiB 作为自动增员上限。

## 3. 当前工具实测结论

实际镜像 `bbx-peer-sandbox:20261120`，ID 为 `sha256:8a103677e66db5fb6a45362ea509f62241167e9484c3bda0205c5f3f9e6deadb`，Linux/amd64，Docker 报告约 4.16 GiB。临时容器为 4 核/6 GiB、network none，模拟 UID/GID 10000 与当前 Worker PATH；Web 夹具只在容器内随机 localhost 端口运行。

- 已功能验证：GCC/GDB、pwndbg/GEF、checksec、ROPgadget、radare2、angr 最小 symbolic step、Sage factor/LLL、Z3、常见密码库、Ghidra headless、JADX 绝对路径反编译、ffuf/gobuster 本地 HTTP、tshark 离线 PCAP、PNG/压缩/steghide 等典型操作。
- 明确缺项：nmap、sqlmap、dirsearch；默认工具面中的 Scapy/OpenCV、Volatility；ffmpeg/sox/multimon-ng、zbar/qrencode、bkcrack/zsteg；SecLists/rockyou 等常用字典。
- 路径/环境差异：John 在 `/usr/sbin/john`；Ghidra headless 不在默认 PATH；JADX 使用绝对输入/输出路径；NumPy/SciPy 在 `/opt/sage/bin/python`，不在默认 Python 3.14。不能把这些解释器差异当作全部未安装。
- 条件能力：APKTool/dex2jar、部分文件取证仅启动/版本验证；Hashcat 仅验证 CPU OpenCL 枚举，未做完整破解负载。其设备自报内存不是容器配额。Volatility 2 笔记不能直接声明为本镜像的可用命令。

工具来源只查阅了知识库的 README、工具目录、脚本依赖及音频/图片工具索引，没有审核全部 WP。没有安装缺项，也没有把题目特定脚本/答案导入比赛包。审计中的初次命令参数或相对路径错误已纠正复测，原始与修正证据均保留。

## 4. 如何验证与实际检查

`make check` 完整通过：Ruff、Pyright、5 项 Pi 测试及 2331 项 Python 测试；475 项 integration/live 用例按规则排除。源码未变，本次未重新运行前端测试或声称新独立核心已通过测试。新增设计文件的本地链接与 `git diff --check` 检查通过。

容量与工具研究脚本/原始证据保存在本工作树 `.data/ctf-standalone-design-20261120/`，不作为产品代码提交。可在本机复现：

```sh
make check
.venv/bin/python .data/ctf-standalone-design-20261120/run-session-capacity.py
.venv/bin/python .data/ctf-standalone-design-20261120/run-session-capacity.py --long
.venv/bin/python .data/ctf-standalone-design-20261120/tool-audit/run_inventory.py
.venv/bin/python .data/ctf-standalone-design-20261120/tool-audit/run_probes.py
.venv/bin/python .data/ctf-standalone-design-20261120/tool-audit/run_followup.py
.venv/bin/python .data/ctf-standalone-design-20261120/tool-audit/run_final_targets.py
git diff --check
```

这些命令复用本机已有镜像；没有构建新镜像、修改现有服务或消耗正式参赛机会。临时容器由 testcontainers 清理，无宿主固定端口。未读取 `.env`，未修改全局配置，未发送钉钉消息。

## 5. 偏差与待决

- 尚无新独立比赛程序、正式镜像或实际解题吞吐结果。资源阈值、批大小和 watchdog 时间窗口是起始工程参数，须经混合负载及故障注入校准。软监控与估算不能保证任何未知工具都不瞬时 OOM。
- 官方手册没有明确镜像平台架构、入口命令、额外模型环境变量/CA 和工作目录重建保留合同。这些在发布前核实；本地核心和模拟接口可以先开发，不猜测变量名，也不提前宣称可参赛。
- 当前工具镜像与会话基准的 Node 版本不同。最终比赛镜像必须固定一个已验证的 SDK/Node 组合，再重做完整组合验收，不能直接拼接两份报告当发布证明。
- 本轮新增 Lead 增员权限和自动援助已经获得用户明确指令；没有额外业务问题需要重复确认。现有已冻结任务不原地迁移。

## 6. 提交与下一步

设计独立提交：`docs: define standalone CTF contest runtime and resource controls`。

范围为三份设计正本、本次主方案、资源保护、工具与提示词、旧 Spec 的覆盖说明及本报告。正文末尾使用 `Implemented by Codex (gpt-6-sol) for task CTF-standalone-20261120.`。前次初步评估报告与 `.data` 研究产物不混入本次提交；本轮不合并 main、不推送。

没有修改根依赖、锁文件、Makefile、Docker Compose、共享 contracts 或产品代码。下一阶段按主方案 S1–S4 实施：本地协作核心 → Pi 与本地工具 → 官方赛事适配和单镜像 → 容量/故障/工具组合验收。补工具时使用明确版本和正式 Worker 身份验证，再由 manifest 生成提示词。

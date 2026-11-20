# 独立 CTF 比赛程序实现与集成验收报告

任务 `CTF-standalone-runtime-20261121`，分支 `codex/ctf-standalone-image-20261120`。本报告记录独立程序、完整比赛镜像及离线验收，继承已提交的工具基底 `1f71594` 和设计决策 `567e5ba`。

## 1. 完成内容

1. **S1 黑板与调度。** 单进程 SQLite 保存题目、成员、Fact、Intent、不可变文件证据、候选、真实回执和持久事件。初始 20 名同级 Worker，平台首次分配，后续实时通知自选，新题优先援助。每题最多 10 人，每人最多 3 条 Intent。困难题默认主责和援助各一人，独立可执行方向可以触发增援。累计解题 30 分钟触发定时事件，排除待领取、核验和暂缓时间。
2. **S2 会话与执行。** 固定 Pi SDK 1.5.0，复用持久会话和跨题上下文。初始 Worker 创建会话记录，收到工作才调用模型。每次模型请求固定执行身份，旧响应无法把工具结果写入新题或新环境。使用原生 read/write/edit 和本地 bash，长输出落盘，支持超时、正常取消、进程组清理与 Linux 崩溃恢复。自定义名称的模型和赛事凭据也从命令环境过滤。
3. **S3 比赛接入。** 实现题目目录、启动、附件、3 个在线环境名额、候选提交、验收、暂缓恢复及显式结束。以真实 `correct`、`correct_flag_count`、`is_completed` 等字段确定进度。部分 flag 接受保留团队继续求解。未知提交仅做读取核对，确定技术失败保留重试能力。失效 attempt token 停止本地运行。
4. **S4 协调与恢复。** 黑板提交后实时处理事件；通知保存精确身份、文本、版本和任务归属，失败重投同一 envelope。首次分配通过持久事件恢复，过期通知被丢弃。Lead 收到待核验即可研判，同题成员收到 `steer`。通过后逐个等待旧执行结束再释放。全部题目暂缓时提醒空闲 Lead 作出恢复或结束决定。环境重开跨 pending/unknown 仅在确认就绪后增加一次代次。结束请求先持久保存意图，断链后保留 unknown 并停止本地，重启通过 GET 核对。
5. **S5 提示词与资源。** Lead 接收当前赛事帮助、目录及资源工具，按实际占用决定增员。Worker 系统提示词包含角色职责、Fact/Intent 操作、当前执行目录和镜像内实际工具及解释器路径。资源采样提供 cgroup CPU 配额、使用量与 throttling、内存、文件缓存、磁盘、事件循环和在途会话/工具。容量管理沿用 Lead 自主决定方案。
6. **S6 独立交付。** 运行包位于 `services/ctf-standalone/`，同一容器包含 Node 程序、SQLite、Pi 会话和解题工具。运行状态保存在工作目录，可按同一 attempt 恢复。完整镜像新增生产依赖层，复用已验证的工具基底。官方 connectivity_test 模式从 description 求解，省去环境和附件调用，确定判定后直接结束本地。

## 2. 验证与结果

所有模型响应来自脚本化客户端或本地 HTTP 夹具。真实 Pi SDK 执行工具调用和会话持久化。验证分为实际工具功能、会话成本、固定组合负载和合成完整求解流程。

### 2.1 离线回归

最终 `make check` 已通过，包含 Ruff 格式与静态检查、Pyright（0 error/0 warning）、原 Pi 5 项测试、Python 2331 项通过及独立程序 110 项测试。宿主跳过 1 项 Linux 专用崩溃清理测试，目标镜像重载后 110/110 全部通过。Python 的 475 项 integration/live 按仓库规则排除，最终 Python 测试耗时 219.27 秒。验证脚本另行通过 Ruff 与 Linux 目标 Pyright。日志保存于 `.data/ctf-standalone-runtime-20261121/make-check-final.log`。

正常 CLI 子进程测试使用显式 JSON 配置和合成密钥环境变量，从实际 `src/main.mjs --config` 启动默认 20 人，完成取题、工具执行、Fact、候选、核验、提交和显式结束。断言退出码为 0、SQLite 状态正确且运行锁可以重新取得。

```sh
make check
npm --prefix services/ctf-standalone test
.venv/bin/ruff check services/ctf-standalone/scripts
.venv/bin/ruff format --check services/ctf-standalone/scripts
.venv/bin/pyright --pythonplatform Linux services/ctf-standalone/scripts
git diff --check
```

### 2.2 完整镜像与负载

验收使用 Linux amd64、Node 22.21.1、4 CPU、6 GiB 内存、512 PID、UID/GID 10000:10000 的临时禁网容器。源码、依赖锁和入口脚本逐文件 SHA-256 与镜像内容比较。每档负载使用独立容器。

会话基准运行 1、8、16、20、32、64 个持久 Worker，每人 16 回合。组合负载在相同人数档位执行每人 2 次真实本地 CPU/文件任务，记录吞吐、失败率、Node RSS、容器内存、事件循环和 CPU throttling。合成完整流程包括默认 20 Worker 求解 20 道本地夹具题，保存 Fact、核验和提交，最终显式结束。

完整镜像首次执行 109 项 Node 测试通过，实际进程组崩溃恢复测试通过。补充 CLI 流程后，归档重载的镜像再次执行 110/110 项测试通过，测试计时约 33.82 秒，容器整体耗时 34.911 秒。其中 CLI 正常流程约 2.13 秒，20 Worker 合成完整流程约 15.93 秒；该时间来自功能回归运行。入口 `--help` 检查通过，65 项工具功能检查通过，工具检查容器耗时 43.743 秒。15 个受检文件（源码、生产包清单/锁文件、运行锁脚本）与构建前 SHA-256 清单一致。镜像 ID 为 `sha256:da01090f74ee8c3f86f38d0177c32e0745f4deca49a1df9b1b159a98f29ea2d2`。

六档会话基准及六档组合负载共 12 个容器 case 全部通过，失败数均为 0。20 个会话的创建耗时为 63.0 ms，该计时从 SDK 已加载后开始。组合负载每次运行 PBKDF2-SHA256 100,000 次迭代与 4 KiB 文件写入读回，每人完成 2 次。

| Worker 数 | 组合任务数 | 组合耗时（秒） | 吞吐（任务/秒） | cgroup 内存峰值（MiB） | 事件循环 p99（ms） |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 0.213 | 9.38 | 112.8 | 27.5 |
| 8 | 16 | 0.618 | 25.88 | 153.4 | 104.9 |
| 16 | 32 | 1.307 | 24.48 | 176.4 | 366.2 |
| 20 | 40 | 1.265 | 31.63 | 187.1 | 520.9 |
| 32 | 64 | 1.802 | 35.52 | 260.8 | 482.3 |
| 64 | 128 | 3.752 | 34.12 | 225.3 | 1485.8 |

上述数值来自各档一次短样本。内存采用内核 `memory.peak`，100 ms 采样可能漏掉短命子进程峰值。启动错峰与短命进程使内存峰值不随人数严格递增。64 人在该固定负载下吞吐低于 32 人，事件循环尾延迟增加，因此增加人数的收益取决于实际工具和等待比例。此结果保留初始 20 人、Lead 根据资源决定增员的既定规则，真实重型解题工作仍需测量。

完整记录位于 `validation/validation.json`，包含 CPU 使用量、throttling、Node RSS、文件增长和每档原始数据。基准期间已等待仓库检查结束，并将镜像压缩排在基准之后。

### 2.3 上传制品

构建前以工具基底上传包 2,787,377,598 字节和实测生产依赖压缩增量约 27.4 MB 做预算，预测完整包约 2.815 GB。最终完整上传包实测 **2,812,089,759 字节（2.812 GB）**，严格小于 3,000,000,000 字节，余量 187,910,241 字节。与工具基底相比增加 24,712,161 字节。本轮沿用工具精简清单，新增完整独立程序仍满足上传体积。

| 项目 | 实测结果 |
| --- | --- |
| 镜像 tag | `bbx-ctf-standalone:20261121-r1` |
| 镜像 ID | `sha256:da01090f74ee8c3f86f38d0177c32e0745f4deca49a1df9b1b159a98f29ea2d2` |
| 系统与架构 | Linux amd64 |
| Docker inspect Size | 2,830,361,166 字节 |
| docker save tar | 2,830,383,616 字节 |
| 完整 tar.gz | **2,812,089,759 字节** |
| 归档 SHA-256 | `0c4af35e430ddb459a7206fb9c10cd5b210b1a3aee50ec9ab2e2e34631aba3bb` |
| 工具 manifest SHA-256 | `253771155d8cf28dd11676e21f26b2114d5a3fa4b415d7648488af43ba62b47b` |

完整归档位于 `.data/ctf-standalone-runtime-20261121/bbx-ctf-standalone-20261121-r1.tar.gz`，同名 `.json` 保存流式导出、gzip 完整性、单 tag、配置身份和层存在性校验结果。该导出回执的 `docker_load_verified:false` 表示脚本只负责导出验证；独立 `load-verification.json` 已记录重载成功、同一镜像 ID、15 文件匹配和工具清单一致。加载后 `validation-loaded/validation.json` 关联入口与 110 项程序测试。本任务临时测试容器已自动清理，镜像和归档保留。

```sh
docker buildx build --platform linux/amd64 --provenance=false \
  --output type=image,name=bbx-ctf-standalone:20261121-r1,compression=gzip,compression-level=9,force-compression=true \
  services/ctf-standalone
.venv/bin/python services/ctf-standalone/scripts/validate-runtime.py \
  bbx-ctf-standalone:20261121-r1 \
  .data/ctf-standalone-runtime-20261121/validation
.venv/bin/python services/ctf-standalone/image/export_image.py \
  bbx-ctf-standalone:20261121-r1 \
  .data/ctf-standalone-runtime-20261121/bbx-ctf-standalone-20261121-r1.tar.gz
docker image load --input \
  .data/ctf-standalone-runtime-20261121/bbx-ctf-standalone-20261121-r1.tar.gz
```

## 3. 偏差与待决

- 原三份设计文件的通用引擎实现使用 Python/MAF 和外部数据服务。本次按已授权独立比赛任务实现单体 Node/Pi/SQLite，内部数据结构局限于该程序。原平台跨服务合同继续位于 `packages/contracts`。
- 本地 read/write/edit 复用 Pi SDK。bash 采用小型本地封装，以支持持久进程组恢复和完整输出日志，显示最后 48 KiB，并提供全文路径。该实现对资源文档原先建议的 BashOperations 接口作了最小替换，保留显式超时、取消及实际退出回执；已通过 Linux SIGKILL 恢复测试。
- 配置采用显式 JSON 加密钥环境变量引用。赛事仍需明确模型配置注入字段、镜像 ENTRYPOINT/CMD 覆盖方式、证书交付、工作目录挂载与持久性，以及最终架构与上传校验口径。当前适配边界见独立程序 README。
- `finish` 请求断链后的官方通用 invalid_state 无法单独证明服务器完成了结束操作。本地保留 unknown，GET 对账结果如实记录，保持原结束决定。探针未知提交同样保留未知结果。
- 工具基底沿用已批准的精简组合。保留 Sage、Ghidra、pwndbg，异构程序模拟、APK 资源重打包和 Sage 网页交互能力变化见工具镜像报告。程序集成后的空间预算和最终体积一并记录。
- 当前性能数据来自合成固定负载，真实模型延迟、CTF 解题成功率和重型工具混合负载下的长期人数容量仍需独立评估。

## 4. 提交划分与共享文件

按以下划分提交，提交前统一执行 `make check`。前两项分别为 `755df74`、`7995683`，第三项包含本报告。

1. `feat: add standalone CTF blackboard and persistent worker sessions`。包含包及依赖锁、黑板、会话、本地工具、提示词、资源与证据模块，以及对应测试和运行锁。
2. `feat: integrate standalone CTF arena lifecycle and recovery`。包含赛事客户端、配置、协调器、CLI、假赛事及完整流程测试、示例配置、README、任务说明和 Makefile。
3. `build: validate complete standalone CTF contest image`。包含 Dockerfile、dockerignore、基准与完整验收脚本、本报告。

共享文件仅修改根 `Makefile`，追加 `ctf-install`、`ctf-check`，并将独立包离线测试加入 `check`。根 Python 配置、锁文件、Compose 与跨服务合同没有本任务变更。验证原始记录和归档保存在忽略的 `.data/`。

## 5. 下一步

取得赛事正式注入样例后，对当前配置完成部署映射，并在明确授权的真实模型评估中测量得分、解题耗时、工具组合和资源峰值。利用这些数据决定 Lead 增员策略和进一步工具精简。

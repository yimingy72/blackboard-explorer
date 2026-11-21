# 独立 CTF Runtime

对应任务 `CTF-standalone-runtime-20261121`，公开接入材料核对日期为 2026-11-21。完整镜像与规定资源下的最终验收结果记录在任务报告。

## 运行组成

Node.js 主进程承载本地 SQLite 黑板、调度器、Lead、20 个初始 Pi Worker 会话、赛事 HTTP 适配器和本地命令工具。Lead 拥有 `grow_workers` 权限和主动增员义务：结合 cgroup 资源、未认领任务、assembling/援助需求、空闲 Worker 与活跃执行判断容量。待援助且无空闲 Worker、任务可能积压，或资源仍有安全余量且存在有效并行工作时，应及时增员；增员沿用同级成员池。

- 首次工作由平台分配，后续有可领取工作时平台实时通知空闲 Worker 自选。新题优先于援助。
- 比赛目录中 `container=false` 的离线题优先是 Lead 的调度策略。Lead 使用 `arena_prepare_offline` 尽早创建缺失题、设置更小的 priority（通常为 0 或 1）、启动离线环境、下载全部附件并写 manifest；平台不替 Lead 硬门禁离线/在线启动顺序，也不本地替 Lead 排在线题目。
- 在线环境同时最多保持 3 个是赛事 API 配额，`pending`、`active`、`unknown` 和 `closing` 均按真实占用状态处理。API 返回配额拒绝时必须保留原始错误并由 Lead 调整；只有题目完成并确认环境关闭，或暂缓时显式关闭环境并收到关闭确认，才视为补位。`arena_start` 不自动下载附件，Lead 必须在环境就绪后通过 `arena_files` 查询并补取 Worker 需要的文件。
- 每题最多 10 名 Worker，每名 Worker 同时最多持有 3 条 Intent。困难题以主责和援助各一人起步，组队阶段主 Worker 立即做低成本侦察和自动化，不等待第二名成员。第二名 Worker 加入前不累计正式解题时间，可并行方向能够继续触发增援。
- 新未认领任务始终高于援助任务。困难题先分配一名主 Worker 并进入 assembling/需要援助。只有没有新题且存在空闲 Worker 或 Lead 增员后，才加入援助 Worker。available 中统一按 Lead 设置的 priority 从小到大选择，离线优先不由 Board 额外硬编码。
- 累计解题达到 30 分钟时，无援助的题开启援助，已有援助的题通知主责研判。
- Fact、Intent、候选和环境代次保存在题目黑板。文件证据通过不可变快照关联任务、作者和执行身份。
- 任一成员可提交候选，平台用 Pi `steer` 通知同题团队和 Lead。Lead 授权赛事提交，程序按真实回执更新题目。成员在旧执行结束后释放，后续题目复用原 Pi 会话。
- Lead 可以暂缓和恢复题目，并协调赛事环境名额。工具与依赖在镜像构建阶段准备。

完整平台的 Web 管理页面、账号服务和外部数据库属于原平台。独立运行包的运行数据位于配置的本地工作目录。

## 当前启动合同

程序要求 Node.js 22.21.0 或更高版本，以及提供 `fcntl` 的 Python 3。当前开发启动支持 Linux 和 macOS，Python 辅助进程持有工作目录的系统锁。已固定的 Pi SDK 和 npm 依赖由 `package-lock.json` 管理。开发环境在本目录执行以下命令安装依赖。

```bash
npm ci --ignore-scripts --no-audit --no-fund
node src/main.mjs --help
```

模型配置必须由显式 JSON 文件提供。可复制 `config.example.json`，填写实际 provider、模型 ID、API 协议、模型接入地址、上下文长度和输出上限。`apiKeyEnv` 填写运行环境中存放模型密钥的变量名。

```json
{
  "workDir": "/workspace/ctf-run",
  "model": {
    "provider": "contest-provider",
    "id": "replace-with-model-id",
    "api": "openai-completions",
    "baseUrl": "http://replace-with-model-service/v1",
    "apiKeyEnv": "CTF_MODEL_API_KEY",
    "contextWindow": 131072,
    "maxTokens": 8192,
    "reasoning": true,
    "thinkingLevel": "max",
    "thinkingLevelMap": {
      "max": "max"
    }
  }
}
```

Pi SDK 只有模型显式声明 `thinkingLevelMap.max` 时才把 `max` 保留为受支持等级；缺少该映射会把请求降为 `high`。因此 `thinkingLevel: "max"` 与 `thinkingLevelMap: { "max": "max" }` 必须一起配置，百智云 OpenAI 兼容接口会收到 `reasoning_effort: "max"`。`xhigh` 也遵循相同规则。

`CTF_MODEL_API_KEY` 是本程序示例配置选择的变量名。赛事方模型注入字段仍需接入时核实。凭据由运行环境提供，JSON 仅引用变量名。

```bash
node src/main.mjs --config /absolute/path/config.json
```

同一个显式配置路径也可通过 `CTF_CONFIG_PATH` 提供。默认工作目录为 `/workspace/ctf-run`，程序在启动时持有该目录的独占运行锁。

当前 Dockerfile 的入口为 `/usr/local/bin/node /opt/ctf-runner/src/main.mjs`，进程使用 UID/GID `10000:10000`。运行时需要提供配置文件，并保证工作目录可写。以下为完整镜像构建完成后的本地启动形式，镜像标签和挂载路径由部署者填写。

```bash
docker run --rm --name bbx-ctf-local \
  --env ARENA_SOLVER_TOKEN \
  --env ARENA_SOLVER_BASE_URL \
  --env BENCHMARK_BASE_URL \
  --env ARENA_ATTEMPT_ID \
  --env CYBER_ARENA_HELP_MARKDOWN \
  --env CTF_MODEL_API_KEY \
  --mount type=bind,src=/absolute/path/config.json,dst=/config/runtime.json,readonly \
  --mount type=bind,src=/absolute/path/run-data,dst=/workspace/ctf-run \
  bbx-ctf-runtime:local --config /config/runtime.json
```

## 赛事注入与模式

已确认的赛事注入内容为 `ARENA_SOLVER_TOKEN`、`ARENA_SOLVER_BASE_URL`、同值 `BENCHMARK_BASE_URL`、`ARENA_ATTEMPT_ID`、`CYBER_ARENA_HELP_MARKDOWN` 和 `/challenge/cyber_arena_help.md`。程序读取带地址的赋值格式，也支持帮助只描述字段名、真实地址由独立环境变量注入的格式。多个来源同时存在时必须一致。请求使用 `BENCHMARK_TOKEN` Header。

本地模拟服务可在 JSON 的 `arena` 对象显式设置 `baseUrl` 和 `tokenEnv`。存在赛事注入地址时，显式地址必须与注入值一致。同一工作目录绑定一个 attempt 身份，换场使用新的工作目录。

完整赛事帮助不是静态系统提示词，也不要求 Worker 各自保存一份。程序在比赛启动和恢复时把脱敏后的帮助全文、真实模式、目录、截止时间与 Lead 黑板放入一条 `ctf.notice` 自定义消息，通过 Pi 的 `steer` 注入 Lead 当前回合。Worker 只接收当前任务事件、题内黑板和路径，避免 20 个以上会话重复加载整份比赛规则。

`arena.mode` 支持 `attempt` 和 `connectivity_test`。模式优先从官方帮助首部的 `Rendered for a single attempt.` 或 `Rendered for a connectivity check.` 标识识别，也可由 JSON 显式指定。两者同时存在时必须一致。缺少模式标识的配置沿用 `attempt`。

- `attempt` 对普通在线题和离线题都先调用赛事 `start`。Lead 应尽快准备并推进离线题，但平台不强制本地 gate；在线题启动顺序由 Lead 根据收益、任务和 Worker 状态决定，3 个实例上限以赛事 API 的真实配额和拒绝回执为准。
- 在线题启动后不会自动准备附件。环境就绪后由 Lead 调用 `arena_files` 查询官方文件清单，并按准确路径下载 Worker 需要的附件。
- `connectivity_test` 从题面 `description` 求解，直接提交候选。探针省去赛事 `start` 和 `close`。赛事记录确定判定后关闭探针，本地按提交回执结束。
- 模式判断使用配置和正式 bootstrap 标识，题目名称与题型字段只用于解题和难度判断。

工具镜像中的 `/opt/ctf-tools/worker-tools.md` 提供精简工具与解释器路径，`/opt/ctf-tools/manifest.json` 保存完整清单。最终镜像构建会从现有基础镜像的 manifest 出发，以 `worker-tools.md` 中实际存在的命令补齐 command 路径，校验基础文件、二进制和归档工具，保留原有 verification 元数据，更新最终镜像用途与赛事运行能力并重写 `manifest.sha256`，不依赖重新拉取或重打基础镜像。

Lead 和 Worker 的系统提示词加载同一份当前镜像清单。Worker 优先组合高信息量命令、复用事实和方向、持久化证据，并在无新增信息时停止重复尝试、申请援助或请求 Lead 判断是否暂缓。`board_act` 的系统提示列出每个角色可用动作及完整必填字段，避免模型通过失败重试猜测参数。

## 回执和恢复

黑板写入和通知事件先持久化，再投递。投递失败保留原始通知 envelope，重试沿用相同身份、文本、版本和作用域。会话根据事件身份和任务版本处理重复或过期通知。

flag 的确定接受、确定拒绝和未知结果分别保存。多 flag 题按回执计数和赛事进度判断整题完成，部分接受后保留原团队继续解题。确定拒绝产生原因 Fact，原团队继续解题。未知结果先读取赛事进度对账，保留候选和原提交身份。已接受 flag 的重复响应通过进度核对处理。

显式结束保存 `finish_attempt` 状态，覆盖 `pending`、`unknown`、`confirmed` 和 `not_sent`。结束请求出现未知结果后，本地停止并保存现场。重启仅用读取操作核对，保留无法确认的结果。明确技术拒绝记为 `not_sent`，后续显式结束决定可重试。

工作目录中的主要数据如下。

| 路径 | 内容 |
| --- | --- |
| `blackboard.sqlite` | 题目、成员、Fact、Intent、候选、回执及协调器元数据 |
| `events.jsonl` | 跨会话模型、工具、通知和协调器事件 |
| `sessions/<agent>/session.jsonl` | Pi 会话历史 |
| `sessions/<agent>/events.jsonl` | 工具调用和通知恢复记录 |
| `sessions/<agent>/tool-output/` | 完整命令输出及执行恢复信息 |
| `work/<agent>/` | 各会话的本地工作目录 |
| `shared/challenges/<id>/inputs/` | 按赛事文件清单下载的附件 |

正常退出会清理当前模型、工具与子进程并落盘。使用相同 attempt 配置和工作目录可恢复已保存的任务、会话及未决回执。

## 验证命令与范围

在 `services/ctf-standalone/` 执行以下命令。

```bash
npm test
node benchmark/run.mjs --counts 1,8,16,20,32,64 --output .data/benchmark-result
```

测试使用本地 HTTP 假赛事、脚本化假模型和实际 Pi SDK。完整流程覆盖 20 个默认 Worker 的本地命令、Fact、候选、核验、提交和结束，也覆盖跨题会话复用、驳回再解、通知失败和重启。

容器验收使用 `scripts/validate-runtime.py`，在禁外网、4 CPU、6 GiB 容器里执行入口检查、Node 测试、实际工具检查以及逐级会话和组合负载。仓库根目录的 `make check` 执行原平台和新增独立包的提交前检查。

会话基准测量准备及历史记录成本，组合负载测量固定 CPU/文件操作的吞吐与资源竞争。本地假模型流程验证执行合同，真实比赛解题成功率、模型延迟和题型表现仍需单独端到端评估。完整镜像的上传体积和工具可用性以最终导出制品验收报告为准。

## 发布前需补齐的赛事合同

公开手册已经说明 hosted 模式由平台启动 harness，并注入帮助和 attempt token。以下字段仍需赛事平台明确，接入时将它们映射到当前显式配置。

- 平台是否保留镜像 `ENTRYPOINT`，以及启动命令与参数的注入方式。
- 模型 provider、模型 ID、base URL、密钥和推理能力的注入名称与格式。
- 模型代理是否需要额外证书，以及证书与受信任 CA 的交付方式。
- 工作目录挂载位置、UID/GID 权限和容器重建后的持久性。
- 最终镜像架构和正式上传包校验口径。

核对来源为公开参与者手册 `https://cyber-arena.chaitin.cn/manual?tab=participant` 和平台手册 `https://cyber-arena.chaitin.cn/manual`。求解请求只使用当前 attempt 帮助提供的官方 challenges API。

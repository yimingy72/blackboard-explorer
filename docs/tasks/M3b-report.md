# M3b · 裁定、收尾与完整闭环报告

日期：2026-09-24。

## 完成内容

- C.1：真实调度流程验证 derive 的空计数、第二次输入含上次 excluded、裁定 missing 与新意图再派发。测试替身现在检查 messages 和 options.instructions，避免漏看实际模型收到的系统上下文。
- C.2：judge 与 final 流程经过真实黑板/对象存储验证。close 登记在事务锁下排除第二个活动 close；同一 close 的 submit_close 串行，final 上传前确认仍处 closing，防止重复调用覆写已接受报告。
- C.3：成功收尾、预算/空推导/人工停止收尾、错误失败均跑通。主动 release 与 finish 自动释放在 closing 下均不增加 attempts。运行器与 MAF 的硬时限为任务时长 + 交接宽限 + 一分钟清扫余量，避免探索预算截止时切断交接。
- C.4：13 个完整场景全部通过，见下表。
- C.5：新增显式付费 `make e2e`：独立 Compose 项目、随机 localhost 端口、独立卷与控制凭据，创建玩具任务并等待报告/归档；保存状态、事件、报告、工作区压缩包及脱敏日志。默认清理，`--keep` 留工作台复盘。

## 13 个场景及规则对应

| # | 场景 | 验证结果 |
|---|---|---|
| 1 | 顺利完成 | 种子→3 并发 explore→judge met→conclude→真实 release 交接，counted=false/attempts=0→final 报告→finished→归档销毁 |
| 2 | 裁定不通过 | unmet 反馈进入仍运行 explore 的下一次模型输入；静默后再裁定，derive 获得 missing 并提出新方向，后续满足 |
| 3 | derive 两次为空 | 第二次收到第一次 excluded；空计数达到 2，final 报告明确 unmet |
| 4 | 有毒意图 | 三次未完成释放，attempts 达 3，意图 inconclusive，不再派发 |
| 5 | 模型不可用 | 连续三个 runtime_error，failed 并归档，不创建 close |
| 6 | 种子空产 | 两次真正空产，failed、归档，无裁定 |
| 7 | 认领竞争 | 真实事务竞争中一个 holder；失利调度 Agent 正常结束，不制造 runtime_error |
| 8 | 宽限超时 | 注入清扫时钟，取消持有者，grace_timeout、系统 note、attempts+1 |
| 9 | 预算用尽 | 探索金额越界进入 closing，final 仍运行并持久化报告 |
| 10 | 支撑事实被争议 | met→acceptance.reverted→unmet，后续重新裁定 |
| 11 | 裁定期间新声明 | 第一轮在途时新 satisfies 不并发第二个 judge；首轮结束后再裁定 |
| 12 | 重启恢复 | runtime_restart 释放持有意图，attempts=0，重新派发并完成 |
| 13 | 人工停止 | 活动种子 stop→conclude/交接→final 报告→归档登记与销毁 |

三个场景文件配合共享 scenario_support.py，使用真实 PostgreSQL/MinIO/blackboard API、真实 MAF 工具循环与 FakeEnvd。通知通道沿用 M3a 的 ASGI events 轮询适配；真实 SSE 与事件防抖由现有测试覆盖。

## 实际检查

- `make check`：**304 passed、35 deselected**（4.61 秒），Ruff/格式/Pyright 全绿；没有真实模型请求。
- `make test-integration`：**31 passed、308 deselected**（86.83 秒），含全部 13 场景、既有容器/存储/锁/镜像检查。
- 单项回归确认 closing 下主动 release 的 `counted=False` 和 `attempts=0`；等待条件要求所有相关 Agent 都进入 concluding，避免把逐项 conclude 的中间状态误判为失败。
- 首次场景 2/3 的超时是 ScriptedChatClient 漏看系统 instructions，补齐替身输入检查后原强断言通过，没有放宽业务预期。
- 镜像构建曾遇依赖源 TLS 证书错误；未关闭校验。确认既有 runtime/blackboard 镜像的 uv.lock 与当前完全相同后，用已验证缓存离线更新，并为正式 Dockerfile 增加 BuildKit uv 缓存。随后正式 `make image-agent-runtime image-blackboard` 与集成目标重建通过。

## 真实 DeepSeek 检查点

实际命令（已执行，无需用户补跑）：

```sh
make e2e E2E_ENV_FILE=/Users/yym/blackboard-explorer/.env E2E_ARGS='--keep --timeout 1800'
```

uv 子进程按用户指引加载本地模型配置，未显示或修改密钥；隔离控制凭据在内存中随机生成，不保存到 Compose 文件。普通检查不加载 `.env`。

最终运行：任务 `25ca10e4-8e1e-46cf-8901-82e165f0ea6c`，Compose 项目 `bbx-e2e-f7903553`，**make 退出码 0**。

- 状态 finished，**A1/A2 均 met**；5 条事实、2 条意图、4 个 Agent 全部结束。
- I1 closed、attempts=0；I2 因 closing 主动 release，事件 counted=false、attempts=0，保留交接说明。
- 报告 **6233 字节**，使用完整 evidence URI；workspace 归档 **5032793 字节**，已实际流式读取归档目录验证包含源代码与 Agent 文件。
- 用量：缓存命中 660736、未命中 74499、输出 36622（含推理 17535）；按固定 profile 高峰价估算 **USD 0.070260516**。未核对 DeepSeek 控制台实付金额。
- 浏览器真实登录并观察 running/closing/finished、2/2 已满足、实时图谱，以及报告和归档入口。报告页面读取正常。
- 核心发现是 Inventory.reserve 无锁 check-then-act；判定和证据由真实 Agent 产生，报告记录了可复现方法与对照验证。本任务不改玩具系统源码或开展 EVAL-2。

首次真实闭环也达到 finished/2 met，但暴露 e2e 导出错误地调用 evidence 接口和主动 release 计数问题；两者已修，报告/归档从正确接口恢复保存，旧栈已清理。正式结果以上述退出码 0 的复跑为准。

产物保存在 `.data/e2e/bbx-e2e-f7903553/`，合并后复制到主工作树 `.data/checkpoints/m3b/e2e/bbx-e2e-f7903553/`。最终工作台 `http://127.0.0.1:55013` 暂留给紧接着的 M4 浏览器验证；runtime、egress-proxy、eval-targets 已停止，不再发起模型调用。M4 结束后清理余下三项服务与卷。所有独立 testcontainers 已清理。

## e2e 用法与边界

先构建前端和四个镜像，再 `make e2e`；更改实现后重建对应镜像。`E2E_ENV_FILE` 可省略，此时从进程环境取密钥；`E2E_ARGS='--keep'` 保留完成后的工作台。`--timeout` 限制任务等待，每条 Compose 命令另限 180 秒。默认停止服务、按完整 task 标签清理 Docker SDK 创建的执行容器、再 down -v；前一步失败也继续尝试后续清理。

## 共享文件、偏差与待决

- Makefile 新增 e2e 目标；runtime/blackboard Dockerfile 复用锁定依赖缓存。无新增 Python/前端依赖、无 uv.lock 变化。
- runtime runner、supervisor、tools、ScriptedChatClient、e2e 与测试；blackboard 只修活动 close 排他和 closing 主动 release 计数；close 模板强调完整证据 URI。
- 设计先单独提交：231f66a（交接时间余量）、8c5eeab（报告防重复覆写）、fcd5440（完整证据引用）。
- 固定价格仍为估算，M5 的评估/调参需区分高峰和错峰。旧提交的报告不会被回写。
- v1 部署级全局出网白名单的界面说明仍在 M4 补；EVAL-2 仍由用户人工完成，M5 前必须确认已合并。

## 建议提交划分

1. `Harden close handoffs and add full lifecycle scenarios`：领域规则、运行器/工具/模板与 13 场景及回归。
2. `Add isolated DeepSeek end-to-end checkpoint`：e2e、Makefile、Docker 缓存、README、报告。

合并后打 m3 标签，更新 HANDOFF，继续 M4。

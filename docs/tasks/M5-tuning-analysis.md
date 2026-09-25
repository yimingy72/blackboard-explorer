# M5 真实评估调优分析

日期：2026-09-25。范围：分析 `eval/results/real-20260925-01/` 与 `.data/checkpoints/m5/real-20260925-01/`，给出第一轮调优。未读取 `.env`，未调用真实模型，未重跑评估。

## 子代理状态

用户要求调用 `gpt-6-astra high` 子代理分析调优；本轮 `spawn_agent` 返回 `agent thread limit reached`，当前可见代理列表只有根代理。为避免丢失 raw 结果上下文，未创建不可靠的外部项目任务，改由主会话完成同一分析。

## 已由数据支持的问题

| 问题 | 证据 | 结论 |
|---|---|---|
| default 多 Agent 没跑赢 single | default 平均 recall/precision/replay/time/cost 为 0.9333/0.7514/0.45/287.9s/0.187192；single 为 1.0/0.7631/0.85/195.1s/0.088848。 | 当前多 Agent 机制没有产生 M5 需要的收益。 |
| default 的额外覆盖没有转化为答案满分 | default 5 次接口覆盖均 14/14，但只有 run-002 满分；半分项为 run-001 P5、run-003 P3、run-004 P2、run-005 P4。single 一次覆盖 12/14 仍 P1–P6 满分。 | default 更会扫接口，但不够会把证据对齐到答案触发形态。 |
| close 裁定过宽 | 半分项均被最终验收接受：P5 用 SQL 500 代替越权成功响应，P3 用代码级 race 代替并发输出，P2 用顺序/超额现象代替并发全额退款，P4 用顺序累计超额代替单次超额或负数边界。 | close 需要证据强度门槛，而不是只看“定位+机制”。 |
| derive/多 Agent 容易放大旁枝 | default 产生平均 3 个 intent、8.2 个 Agent、450 个事件；single 平均 0.8 个 intent、3.6 个 Agent、196 个事件。default 多次追“匿名券目录”“重复领取同码”“重复传同一券 id”等业务规则未给出的方向。 | derive 应只针对裁定缺口补证，避免把可疑业务规则当新漏洞扩散。 |
| 可复查脚本规范不足 | default replay 失败主要来自旧 token、预置用户、旧绝对路径 `/workspace/shared/repo`、旧服务状态；single 仍有旧 DB 路径/预置用户依赖。 | 发现事实应附可移植脚本：从 seed 或脚本自建数据出发，不依赖旧运行状态。 |
| 调度即时裁定带来成本 | default 平均 close 3.6 次，run-005 有 6 次 judge；single 平均 close 2.4 次。 | `pending_claims` 立即触发 close 在多 Agent 下成本高，且会在证据未充分时提前接受。 |

## 合理推断

1. default 的弱项不是“读不到代码”，而是证据质量控制。它能列出接口并找到大多数机制，但不会稳定地区分“代码级风险”“相邻现象”和“答案要求的动态触发”。
2. single 表现好，可能因为单个 Agent 把目标当成一个整体审计，少了 intent 拆分后的语境损失和重复裁定；default 被 derive/多探索拆成多个局部问题后，出现旁枝和证据口径不一致。
3. 当前验收 A1/A2 比隐藏评分 P1–P6 更粗，close 没有隐藏答案，只能按通用证据规则判断。因此调优不应写入 P1–P6 关键词，而应强化通用证据门槛。

## 第一轮调优已做

本轮只改 profile 提示词，不改设计正本、不改隐藏答案、不改评分规则。default 与 single 同步修改，保持二者提示词一致，后续仍可比较 derive/并发机制本身。

1. `profiles/*/prompts/explore.md.j2`：
   - `satisfies` 只能在证据同时覆盖位置、触发条件、实际结果/危害时填写；代码推测、500、顺序请求替代并发、无业务约束来源的可疑行为不能填。
   - 动态问题优先保存可移植复现脚本与运行输出，脚本不得依赖旧 token、旧数据库、旧绝对路径、旧端口或宿主旧服务。
   - 收到裁定缺口后按缺口原样补证，列出并发、注入、金额边界、业务规则四类最小证据要求。
2. `profiles/*/prompts/close.md.j2`：
   - met 前必须逐项核对证据强度。
   - 并发类必须有并发请求输出和结束后状态；注入类必须有成功改变结果/越权读取/数据外泄；金额边界类必须有对应非法值响应；业务规则类必须说明规则来源。
   - 只满足定位和机制时判 unmet，并在 missing 中写最小补证。
3. `profiles/*/prompts/derive.md.j2`：
   - 只针对 unmet 和裁定缺口提出 intent；已经 met 的验收项不补旁枝。
   - 若缺口是证据强度不足，method 必须直接补最小动态证据。
   - 缺少业务约束来源的方向写入 excluded，不作为漏洞方向提出。

## 还不应立即做的调优

1. 不应把 mini-shop 的 P1–P6 触发器写进提示词；那会污染评估。
2. 不应为了提高 replay 人为恢复旧 token/旧 DB/旧绝对路径；这会掩盖脚本不可移植问题。
3. 不应只靠降低并发让 default 变成 single；先测证据门槛是否已经减少误判和旁枝。

## 下一轮最小实验设计

| 实验 | 对照 | 次数 | 成功标准 | 失败后动作 |
|---|---|---:|---|---|
| E1：提示词证据门槛调优 | 新 default vs 新 single，二者提示词相同，仅 derive_enabled/并发预算不同 | 各 3 次起步；若差距变小再扩到各 5 次 | default recall 不低于 single；default 半分项降到 0；default replay 平均 ≥ 0.8；成本不超过 single 1.5 倍 | 若 default 仍输，说明主要矛盾不是提示词证据门槛，而是调度/并发拆分。 |
| E2：裁定节流 | E1 中较好的 prompt；default 开启“静止或批量后再 judge”的实验分支 vs E1 default | 各 3 次 | close 次数下降 30% 以上，recall 不降，耗时/成本下降 | 若召回下降，保留即时裁定但要求 close 返回更具体 missing。 |
| E3：derive 收敛 | derive 只允许根据 unmet missing 生成 intent，且每次最多 1–2 个 | 各 3 次 | intent 数下降，false_positive 不升，半分项减少 | 若漏问题，增加“接口覆盖缺口”例外。 |
| E4：证据产物规范 | 在 explore prompt 和评估任务 domain_context 中要求可移植脚本格式 | 各 3 次 | replay ≥ 0.85，失败原因不再是旧 token/旧路径 | 若仍失败，给 eval runner 增加脚本规范检查或 replay 模板。 |

## 建议提交划分

1. `Tighten profile evidence gates for evaluation`：profile 提示词与本分析报告。
2. 若继续做 E2/E3：单独设计并实现调度参数，先更新设计正本，再改 scheduler 与测试。

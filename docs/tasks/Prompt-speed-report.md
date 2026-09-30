# Prompt-speed · 实施与上下文说明

日期：2026-09-30。按用户对速度审查六项建议的逐项反馈实施：第1、2、6项完成；第3项只解释；第4项只解释且不修改；第5项不实施。主会话负责前端与最终审查，子代理均为GPT-6.1 Sol high。

## 已完成

1. **种子早发布**：default/single Explore模板明确最小验证与保存证据→立即发布Fact→据已发布Fact提出独立Intent、认领最有把握方向→继续长准备。保留证据不足时继续调查、不凑Fact/方向、20步种子上限、60步Explore上限及conclude规则。每5轮提醒不改，没有写入具体题目的调查套路。
2. **任务自由选择思考强度**：模型编辑提供DeepSeek low/high/max，兼容/Responses连接的DeepSeek模型同样可选max；已配置xhigh等兼容值仍可显示、继承和保存。创建表单默认沿用所选模型配置，可显式选择强度，切换模型回到继承；覆盖仅复制到当前任务不可变Profile，三Worker共同使用，不改平台默认、旧任务或续跑。省略/null表示继承，显式none保持既有“不传参数”的含义。不存在默认强制降级。
3. **短暂断连恢复**：仅真实模型connection/timeout传输失败进入应用层恢复。每Agent本次run累计最多2次额外恢复，成功不清额度；同任务/模型恢复episode固定120秒，2、4秒退避，每episode最多两个实际恢复探针且同时只有一个。探针是现有Agent自己的模型请求，没有额外收费ping。等待仍计活动时长，预算、conclude、人工停止和清扫保持可达。SDK原4次重试不变；持续失败仍触发原窗口保护。
4. **恢复安全边界**：一次MAF run失败后丢弃其流聚合器，从现有检查点恢复同Agent/Session/Intent/derive轮次；完整旧工具结果复用，未知结果标记中断，框架不自动重放旧调用。未确认用户消息保留原ID/claim token，失败请求中的黑板更新和结束指令按已确认水位重注入。只有模型调用来源的错误可恢复，heartbeat、会话、控制HTTP、本地及永久模型错误走原错误路径。
5. **派发与可见性**：恢复期间暂停新Explore/Derive/普通judge派发，正在进行的模型请求可以完成；完整成功解除暂停，成功前已开始的迟到失败不重新封锁。final Close每episode最多成功登记一次，避免冷却期每个tick重新创建。控制preflight后、实际模型开始前重新检查准入，防止突破单探针。Agent记录显示连接恢复及累计次数，恢复trace使用真实失败调用步数。

接口变化仅为TaskCreateBody.reasoning_effort可选字段、Provider目录reasoning_efforts及相应OpenAPI/前端类型；无数据库迁移、新依赖、价格、工具权限或并发策略变化。

## DeepSeek max及上一轮判断更正

官方DeepSeek支持low/high/max，medium/xhigh兼容值映射high，max为独立强度。因此不能仅凭配置xhigh标签断言实际使用最大思考，上一轮建议xhigh→high不足以证明能提速。Chat使用reasoning_effort，Responses使用reasoning.effort；本轮实际SDK+MockTransport确认max原样发送，不转换为high。网关端实际映射和性能仍需另行实测。[官方思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)、[Responses API](https://api-docs.deepseek.com/api/create-response/)。

## Derive现在如何填充上下文（本轮不修改）

- 首次启动：OpeningContextProvider将目标、领域背景、验收状态、全部Fact的陈述/状态/证据摘要URI、全部Intent及结果/交接说明渲染到系统指令。包含清单与摘要，证据文件原文仍按需get/read_evidence。
- 后续推导：同一Agent ID和原生Session复用；新增本轮模式、版本、当前验收及上轮excluded控制消息，BoardSync按last_seen_version追加黑板增量。正常情况下不重新把最新黑板完整清单渲染一遍。
- 达到context_threshold：下一推导轮开始新模型输入段，重新渲染当时完整黑板上下文；旧消息仍保存在同一Session供复盘，不删除历史。修改Worker提示词正文时也会按当前黑板重新渲染单份系统指令。
- 请求传输：当前Responses store:false，在每次请求本地回放当前输入段的会话历史，因此请求里仍有初始黑板和历史增量；“追加新增内容”不等于HTTP只发送增量。后台state读取控制状态也不等于每轮重新把完整state注入模型。

代码入口：OpeningContextProvider.render、AgentRunner.run_agent的分段与推导轮次控制消息、DeriveHistoryProvider、BoardSyncMiddleware。最新只读样本Derive最大输入241,541 token、任务阈值800,000，未达到阈值分段条件；缓存96.7%属于当前实现，旧65.6%样本不代表现在。

## Session点的说明（按要求暂不修改）

这是agent-runtime与黑板服务之间的本地HTTP保存路径：运行时序列化完整MAF会话并上传，黑板服务保存到PostgreSQL，再将同一完整Session、初始指令和revision原样回包。正常保存路径运行时主要读取返回revision；丢响应恢复另行GET并逐项核对完整内容。

例如一个会话约1.5MB，现有保存大致是“上传1.5MB→持久化→回传约1.5MB”；提议的小回执只会改成“完整上传并持久化→返回版本/确认信息”。完整消息仍可读取，也不会删掉历史或改变模型上下文。它减少的是重复回包和解析，不是取消保存。

用户要求先说明，所以本轮session.py、blackboard/conversations.py、Session PUT/GET返回、CAS、保存频率及归档格式全部不改。连接恢复只调用已有检查点读回和修复机制。

## 实际验证

| 检查 | 最终结果 | 日志 |
|---|---|---|
| make check | Ruff/Pyright全绿；822 passed、73 deselected，14.24秒 | /private/tmp/bbx-prompt-speed-check-final.log |
| make test-integration | 镜像构建成功；69 passed、826 deselected，191.15秒 | /private/tmp/bbx-prompt-speed-integration-final.log |
| 前端check | ESLint/TypeScript、原33项Vitest通过 | /private/tmp/bbx-prompt-speed-web-check.log |
| 前端build | 成功，9.71秒；既有大chunk提示保留 | /private/tmp/bbx-prompt-speed-web-build.log |
| 浏览器类型检查 | 通过 | /private/tmp/bbx-prompt-speed-web-e2e-types.log |
| 完整Playwright | 原27项及新增5项，共32 passed，25.3秒 | /private/tmp/bbx-prompt-speed-web-e2e.log |

定向验证：种子/热更新等69项；任务强度/Provider/API/快照等133项；模型客户端原+新增max载荷116项；恢复及关联原runner/middleware/stream/session/scheduler185项，恢复+原领域122项；恢复trace步数修正后相关63项。组合有重叠，不能相加为独立覆盖。

新增恢复回归使用真实MAF工具循环和Mock SDK SSE：失败正文/半截工具参数不进入最终回执，完整工具只执行一次，未知工具不重放，用户claim确认一次，失败增量与结束指令重注入，停止/预算可达，累计额度不因成功清零，单探针及preflight竞态、冷却、三窗口持续故障保护都得到验证。永久HTTP/本地/控制链/会话错误不进入此恢复路径。max载荷用例验证Chat/兼容Chat/Responses真实SDK请求字段，无真实HTTP模型调用。

第一轮集成68通过、1项失败：旧用例假定4个失败Agent只会产生4条model_error，而新行为增加2个共享恢复探针。修正为精确6次调用/6条trace，逐Agent对齐；保留并加强4个失败Agent、同窗failure_streak=1、Intent attempts=0和仅一次窗口增量断言，未放宽为“至少4”。单跑通过后完整69项重跑全绿。第一轮前端测试类型检查也暴露MockProfile索引型推导问题，已修正新测试的对象构造，生产代码无此错误。

桌面模型max、390px创建max截图保存在`.data/checkpoints/prompt-speed/`，均为模拟数据；原全站响应式/设置/会话回归通过。没有改真实配置来做浏览器测试，没有live/e2e/eval收费调用、目标操作或.env读写。

复现主要命令：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration
pnpm --dir web check
pnpm --dir web build
pnpm --dir web exec tsc --project e2e/tsconfig.json --noEmit
PLAYWRIGHT_BROWSERS_PATH=/Users/yym/blackboard-explorer/.data/playwright pnpm --dir web e2e --workers=2
git diff --check
```

本机Docker构建使用既有代理及DOCKER_BUILD_ARGS，见使用与部署文档；测试使用随机端口/独立临时容器。

## 偏差、限制与提交划分

- 本轮没有重新测量真实同题完成时间，不能把提示词与离线恢复通过解释为已提速某个百分比。未来对照应保持任务、模型、预算和并发一致；由用户自由选择思考强度。
- 长时间网络不可用仍会终止，恢复不承诺无限等待；等待计活动预算。中断流缺少完整usage时，不能声称本地费用覆盖供应商所有实际扣款。
- 派发上限、Derive普通/完成复核行为、Close裁定和归档规则保持；没有实施Session小回执或增加并发。
- 修复了审查中发现的冷却final重复登记、preflight开始/准入竞态及恢复trace步数，不回避失败测试。
- 96a94a0为设计及任务合同；bc7aa36为种子提示/分支回归；2f93389为任务强度后端+前端/协议回归；89afb78为运行时有界恢复/回归；使用说明及本报告独立提交。检查通过后按既有授权合并main，不推送远程。
- 部署结果在本报告末尾补充，确认无活动任务后更新本地服务，保留用户模型/Worker设置，不自动重跑旧任务。

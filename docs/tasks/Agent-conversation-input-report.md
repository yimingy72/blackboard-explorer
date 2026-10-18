# Agent 手动对话与回车发送修复报告

日期：2026-10-18。基线 `main@e232154`；分支 `fix/agent-conversation-input`；工作树 `/Users/yym/bbx-wt/platform-performance-audit`。

## 原因与修改

1. **公网发送在请求前就失败**：直接打开公网 HTTP 登录页，确认 `isSecureContext=false`、`crypto.randomUUID` 不存在，而 `crypto.getRandomValues` 可用。两个对话组件在错误处理之外调用前者创建消息 ID，因而按钮发送和快捷键都会报错；localhost 被视为可信来源，原本的本机测试未暴露此问题。新增统一 UUID v4 生成函数：优先使用原生方法，否则使用浏览器加密随机字节；不使用弱随机数、不新增依赖。
2. **修复同类入口**：替换两个对话组件以及成员操作、普通/CTF 续跑、初始附件全部六处直接调用。失败重试仍复用原 ID，切换 Agent、布局或关闭详情不会丢失草稿。
3. **Enter 发送**：复用 Ant Design X Sender 的 `submitType="enter"`，Shift+Enter 换行，保留 Ctrl/Command+Enter。统一拦截输入法 composing 和 keyCode 229，覆盖 Safari 在 compositionend 后确认候选词的事件顺序。
4. **已结束普通 Agent 可在任务内复盘**：Explore/Close 原有只读问答保持；取消 inactive Derive 必须等整个任务结束的额外限制，继续复用既有 MAF ChatWorker、持久 Session 和只读工具。复盘不改黑板、执行费用或 Agent 执行状态。
5. **同会话互斥**：复盘 claim 与 Agent 重新激活共用任务行锁；同 Agent 仅能有一个有效 processing 租约，Derive 或失败 Explore 复用 ID 时等待有效复盘结束。调度器仅吸收明确的 `agent_review_active` 冲突并在后续 tick 重试，其他错误继续正常报告。会话 CAS、claim token、租约与执行代次保护保留，过期回复不能覆盖新执行。

运行中的 Agent 仍在下一次模型调用接收定向消息；已结束普通 Agent 使用只读复盘。CTF 活跃成员收到消息可继续正常协作，整个任务结束后继续只读复盘；用户主动停止的成员仍需明确恢复才继续执行，移除与历史回放边界不变。

## 实际验证

- `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：**2221 passed、386 deselected**；Ruff、格式、Pyright 通过。
- `cd web && pnpm check && pnpm build`：**147 passed**，生产构建通过；五个修改组件的 Ant Design CLI 检查均为 0 issues。
- 相关 PostgreSQL 集成：**11 passed**，覆盖任务内 Derive 复盘、同 Agent 双问题互斥、claim/register 竞争、完成/租约过期后的重新激活与旧 checkpoint/complete 拒绝。
- ChatWorker、SessionCheckpoint、scheduler executor 脚本模型检查：**40 passed**，包含调度等待及其他冲突继续抛出。
- 浏览器共 **27 个不同用例通过**：新增六项 HTTP 能力缺失/按钮/Enter/换行/输入法/重试检查，加四项会话连续性、17 项续跑/CTF 生命周期/附件原有回归。最后修改输入法保护后，六项重新通过，未重复计数。
- 部署后的本地与公网各 **6 项浏览器检查通过**：直接加载各自实际发布的生产静态资源，API 用隔离 fixture 拦截，不向真实 Agent 发消息。公网检查确实运行于 HTTP 公网来源。
- `git diff --check` 通过。没有调用真实模型、续跑任务或访问靶场。

浏览器检查命令：

```sh
cd web
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  pnpm e2e conversation-send.spec.ts conversation-continuity.spec.ts --workers=2
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  pnpm e2e resume.spec.ts ctf-lifecycle.spec.ts task-inputs-layout.spec.ts --workers=2
```

后端定向检查命令（仓库根目录）：

```sh
.venv/bin/python -m pytest services/blackboard/tests/test_store_integration.py -m integration \
  -k 'idle_derive_review or concurrent_review_claims or review_blocks_reactivation or review_claim_and_derive_reactivation or conversation_cas_claim or expired_claim or finish_requeues or old_derive_round' \
  -q --tb=short
.venv/bin/python -m pytest services/agent-runtime/tests/test_scheduler_executor.py \
  services/agent-runtime/tests/test_chatworker.py services/agent-runtime/tests/test_session_checkpoint.py \
  -q --tb=short
```

## 本地与公网部署

部署版本：`bbx-blackboard:agent-chat-20261018`、`bbx-agent-runtime:agent-chat-20261018`，迁移仍为 0018。每次切换前在数据库短暂写屏障内确认没有活动任务、待处理对话或正在运行的 CTF 回合；Postgres、MinIO、执行镜像及其他服务均未替换。

- 本地：**19 任务、150 会话、58,663 对象 / 1,677,884,800 字节**保持，20 张业务表及对象清单摘要完全一致。
- 公网：**23 任务、223 会话、111,078 对象 / 9,903,573,559 字节**保持，20 张业务表及对象清单摘要完全一致。
- 两端运行配置、端口绑定及原备份保留；未恢复或重跑原失败任务，预算未改。

公网首次发布因 macOS AppleDouble 元数据被 GNU tar 解出为 `._*.py`，迁移加载时产生 NUL 语法错误。已立即恢复原版服务，清理发布包与镜像继承层中的附加元数据，补做无网络编译检查后重新发布成功；此过程中没有活动任务被中断。最终源包已用 `COPYFILE_DISABLE=1 tar --no-xattrs` 重新生成，明确验证不含 AppleDouble 条目。

备份与检查材料：本地 `.data/agent-chat/`；成功切换备份 `.data/agent-chat/release/update-agent-chat-20261018-20261018T144223Z/`。公网成功切换备份 `/opt/blackboard-explorer/private/update-agent-chat-20261018-20261018T144644Z/`；首次失败发布备份同前缀 `20261018T144223Z` 仍保留。发布源位于 `/opt/blackboard-explorer/releases/agent-chat-20261018`，运维入口已指向当前版本。私有配置快照不进入 Git。

## 偏差与待决

- 无新增依赖、数据库迁移、模型配置或提示词变更；不修改三份设计正本。后续设计同步可补充“普通 inactive Derive 可在任务运行时只读复盘，同会话重激活需等待有效复盘租约”。
- 本轮不扩展 CTF 主动停止/移除成员在活跃任务中的全新复盘执行通道，保留用户已有的明确恢复语义；这与本次 HTTP 发送报错分开。
- 使用脚本模型与隔离 API 验证，不产生真实模型费用；真实服务回答内容由用户测试。

## 建议提交划分

1. `fix(web): restore agent messaging on HTTP origins`：统一 UUID、六处入口、键盘发送和前端回归。
2. `fix(conversations): allow safe in-task derive reviews`：普通模式复盘准入、会话互斥、调度等待及后端回归。
3. `docs: record agent conversation input repair`：任务、报告与交接记录。

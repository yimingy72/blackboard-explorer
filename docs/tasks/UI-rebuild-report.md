# UI-rebuild · B2 重构报告

日期：2026-10-10。工作树 `/Users/yym/bbx-wt/ui-rebuild`，分支 `feature/ui-rebuild`。

> 已按用户确认的 B2 与顶部 Goal 方案完成正式重构，保留现有功能，自动检查通过并已本地启动，交由用户测试。独立设计预览不作为正式验证。

## 1. 已确认的依赖与约束

| 项目 | 已确认事实 |
| --- | --- |
| 既有业务栈 | 保留React 18.3.1、Vite、Query、Router、ReactFlow、原API和Session |
| 通用组件 | antd 6.6.5；官方peer要求React/React DOM >=18 |
| 对话组件 | @ant-design/x 2.9.0；peer要求antd ^6.1.1、React/React DOM >=18 |
| 图标 | @ant-design/icons 6.3.4 |
| 面板调整 | react-resizable-panels 4.14.3；peer兼容React18/19 |
| 本地工具 | @ant-design/cli 6.6.5仅安装在忽略目录 `.data/ui-tools` |
| API资料 | `--version 6.6.5 --format json` 输出保存在 `.data/ui-rebuild/antd-components` |
| 设计依据 | [任务说明](UI-rebuild.md)、[web/DESIGN.md](../../web/DESIGN.md)，独立预览只作设计参照 |

官方资料：[Ant Design兼容说明](https://ant.design/docs/react/migration-v6-cn/)、[XProvider](https://x.ant.design/components/x-provider-cn/)、[Bubble](https://x.ant.design/components/bubble-cn/)、[Sender](https://x.ant.design/components/sender-cn/)、[Think](https://x.ant.design/components/think-cn/)、[ThoughtChain](https://x.ant.design/components/thought-chain-cn/)。

API约束：Form异步快照不依赖initialValues自动更新；Checkbox使用checked，Group才使用数组value；Upload仍接原API；加载和动效不伪造逐token流。CLI的Checkbox结果有Group层级混列，已保存问题预览、未提交外部issue，使用时结合官方API与实际类型。

## 2. 按任务编号完成状态

| 编号 | 交付 | 已完成的实现 |
| --- | --- | --- |
| U1 | 组件、主题、图标与反馈 | 根级 XProvider、中文 locale、Ant App；蓝色主题、系统字体、统一按钮/表单/浮层和减少动态偏好。聊天使用 Bubble、Sender、Think、ThoughtChain。未覆盖内部 `.ant-*` 结构。 |
| U2 | 紧凑侧栏与上下文导航 | 左栏 60/208px 可展开，任务/新建/配置/最近任务集中导航；画布仅在任务详情出现。窄屏导航抽屉、唯一 ID、键盘及路由焦点保持。 |
| U3 | 工作台、详情、会话与 Goal | 默认全画布；Agent 连续会话、题目概览/记录/文件、可拖动/键盘调宽与放大。目标在画布上方展开，支持完整原文、完成要求、背景与附件预览；保持选择、视角、草稿和 Session。 |
| U4 | 全平台原功能保持 | 登录/退出、任务名称/列表/创建/初始附件、模型/Worker/CTF/MCP 配置、角色管理、消息幂等、终态续聊/续跑/删除、历史回放/裁定/证据/归档/报告均保留原业务接口。 |
| U5 | 检查、本地恢复与发布 | Python、集成、前端及80项完整浏览器回归全绿；本地数据库最小恢复、API/runtime 启动，B2 正式构建与散列核对完成。 |

关键交互修复：受控菜单 Esc 只关闭菜单，复盘窗口关闭后返回实际工具栏触发器；附件弹窗退出动画不抢走已收起目标的焦点。手机查看目标仅隐藏原详情，保留会话 DOM 和未发送草稿。未知发送结果跨 Agent 面板重开/899px 断点切换保留同一个请求 ID；迟到响应不清除用户新写的草稿。

画布节点采用专用语义按钮保持多行卡片布局，其他通用控件由组件库提供。完整系统提示词使用 TextArea 公开 textarea 语义样式，避免只给外层设置高度；长配置页保存栏保持可见。详情正文安全 Markdown，推理仅显示服务真实保存的文本或摘要，工具参数与结果可折叠读取。

## 3. 实际验证

所有自动检查使用脚本化假客户端或完全 Mock 的业务 API；没有新建真实任务、发送真实 Agent 消息或调用真实模型。

| 检查 | 实际结果 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check` | Ruff 247 文件通过；Pyright 0 error/0 warning；1090 passed /124 deselected，15.39秒。3条既有 MCP 弃用警告。日志 `.data/ui-rebuild/checks/backend-final.log`。 |
| `make -o image-exec-env -o image-egress-proxy -o image-agent-runtime -o image-eval-env test-integration` | 最终补充 CTF 完成要求字段后重跑：120 passed /1094 deselected，282.39秒；使用现有镜像和随机端口隔离测试。日志 `integration-final.log`。 |
| `pnpm --dir web check` | ESLint/TypeScript 通过；14 文件、48 单元测试通过。日志 `frontend-final.log`。 |
| `pnpm --dir web build` | 生产构建通过；保留 Vite 大于500kB chunk 提示，没有隐藏告警。ELK 按需加载。日志 `build-final.log`。 |
| `.data/ui-tools/node_modules/.bin/antd lint web/src` | 71 文件，0 issue；已使用 Ant6 当前公开语义 API，移除本轮发现的 deprecated props。日志 `antd-lint.txt`。 |
| `pnpm --dir web exec tsc --noEmit -p e2e/tsconfig.json` | 通过，日志 `e2e-types.log`。 |
| `BBX_E2E_BROWSER="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" pnpm --dir web e2e --workers=3 --reporter=line` | 最终完整回归：80 passed，2.1分钟，退出码0；无未Mock API告警或58000回退。详情记录见 `.data/ui-rebuild/checks/e2e-final.log`（最终结果摘要）。 |
| 静态产物及服务 | 12 个发布文件逐一经实际 HTTP 读取，SHA256 与本机构建一致；后端 CTF API 源文件散列一致。 |
| 正常真实页面 | 实际 Codex 浏览器打开正式登录页，确认新版表单；未登录访问 `/api/tasks` 返回401。原浏览器登录会话已失效，没有读取 `.env` 或绕过认证，因此真实登录后的任务/配置页留给用户验收。 |

浏览器回归覆盖1440/1920、768、390px和600px短屏；包括 B2 导航、面板拖动与键盘调宽、原文/Goal/附件与草稿、图谱视角、记录/回放、推理/工具、表单失败与保存竞态、消息未知结果重试及删除/续跑边界。保留原业务断言，额外验证卡片正文位于边界内、真实提示词输入区高度与长页保存栏命中，避免仅检查外壳可见。

完全 Mock 的浏览器下载不能依赖单一请求拦截：Chromium 下载可能绕过 page/context route。本轮新增 `e2e/run.cjs` 随机端口 HTTP 服务，只提供显式登记的附件 bytes/filename；其他未拦截请求返回501并使 runner 失败，不会落到58000生产 API。下载仍通过真实浏览器点击，核对完整字节和文件名。

减少动态偏好、Tab/Enter/Esc、焦点恢复和主要配色对比度都有模拟浏览器断言；没有声称测量生产 Core Web Vitals、所有浏览器、真实手机触控或模型求解质量。

## 4. 本地恢复与发布

正式测试入口：<http://127.0.0.1:58000/login>。使用原 `ADMIN_USERS` 账号，未修改认证或模型凭据。代码位于 `/Users/yym/bbx-wt/ui-rebuild`，分支 `feature/ui-rebuild`；58010仍是设计预览，不是本次交付地址。

### 数据库恢复

原 PostgreSQL、MinIO、blackboard 因此前本地环境异常退出，runtime 持续重启。恢复原PG容器时出现 `replication checkpoint has wrong magic 0 instead of 307747550`。只读检查确认 `pg_logical/replorigin_checkpoint` 恰为8字节全零；复制槽与相关目录为空，origin/subscription heap为空，仓库没有逻辑复制功能。

先完整离线备份原卷至 `.data/ui-rebuild/recovery/postgres-data-before-recovery.tar.gz`（23,777,101字节，权限600，目录700），SHA256为 `2339c6a1f3a9a195c0b2f45e6a90d6e53e528b40af32b908f3924a4195341f09`。原卷只读挂载，备份可读取且包含1409项。

在独立无网络临时卷/容器中验证：只隔离全零文件，PG16正常执行 crash recovery 并接受连接。随后原PG完全停止时核对坏文件与备份一致，仅将其改名为 `pg_logical/replorigin_checkpoint.ui-rebuild.bad`，使用原PG正常恢复；没有重建数据库、改 `pg_control`、清理WAL或执行 `pg_resetwal`。副本容器和卷已删除，完整备份及坏文件保留。

这一最小处理依据[PostgreSQL16.15官方 StartupReplicationOrigin 源码](https://github.com/postgres/postgres/blob/REL_16_15/src/backend/replication/logical/origin.c)：文件缺失时返回，后续 checkpoint 重写状态。恢复后 origin/origin_status/subscription 全部为0。数据页 checksum 未启用，不能宣称验证了所有物理数据页；确认实际恢复和关键应用数据后再启动服务。

### 应用与数据核对

恢复后、应用启动前后只读计数一致：5个任务、11个 Session、7个 Agent profile、2个平台配置、13个CTF成员、98个CTF回合、1588个事件；schema保持0014。活动任务0，没有运行回合。两个 queued 消息属于已结束任务的 turn_finished 通知，没有创建新模型工作。

只启动指定 `bbx-ctf-team-mode` 项目：既有 PG/MinIO 用 `start`，blackboard在确认活动任务0后使用 `up -d --no-deps blackboard` 切换构建，runtime原容器重启恢复。PG/MinIO健康、API/runtime运行。未操作其他Docker项目、删除任务、清理原卷或修改 `.env`。

Compose 对历史恢复卷的标签发出命名差异警告，本轮没有对PG/MinIO执行 `up` 重建。保留原 `bbx-ctf-team-mode-postgres_data` / `minio_data` 卷；后续不得把空的新命名卷当作原数据卷。

### 当前发布

从既有后端镜像构建只含当前 `web/dist` 与4行只读API补充的本地镜像，不安装新运行依赖，构建使用 `--network=none`，仅保留当前 `bbx-ctf-blackboard:latest` 标签，没有建立旧版本回退标签。镜像当前ID：`b2d98f145ae233236251983218a4c21a4534ba4e715e2486a1a118abdb0c6240`。

最后菜单键盘修复采用静态更新：先复制全部新hash资源并保留旧hash供已打开标签使用，再在同一文件系统原子替换index，不再次重启API/runtime。blackboard启动时间 `2026-10-10T14:22:31Z`，runtime `14:23:18Z`，两者restartCount0。HTTP核对12个文件与最新构建完全一致，index SHA256 `2307608f03a5fca037b51d5be10b00cef61049447c9401fba0cef20a119786f7`；清单位于 `.data/ui-rebuild/deployed-assets.json`。

## 5. 偏差与待决

- 为显示真实CTF完成要求，补充人类认证的只读 `/ctf/state` 顶层 `task.completion_requirements`，只接受已有字符串；不透出完整ctf_control、turn、message、Session私有数据。Agent与service返回规则不变，无数据库、OpenAPI契约、调度或设计正本修改。四组参数化API回归覆盖原文、缺失、null和异常对象。
- 当前对话使用服务保存的完整响应 checkpoint，不是逐token流；未增加模型传输协议或虚假思考动画。
- 既有CTF预算准入重复唤醒问题保持独立待修，本UI任务没有将其标为解决；候选也不会由UI变成已通过。
- Vite仍有大型chunk提示；本轮未做生产性能测量或为压数字更换架构。静态产物与响应式功能已检查，真实设备和真实登录后的业务验收由用户执行。
- 最初本地验收检查点尚未提交；用户随后明确要求提交、合并main并推送GitHub，本次已完成代码发布。实际提交记录见第7节。

## 6. 提交划分与用户验收

1. `Expose public CTF completion requirements`：后端只读字段及其参数化授权/隐私测试。
2. `Rebuild frontend with the B2 component system`：web源码、依赖锁、公开语义样式、会话与面板状态保持、全部浏览器适配/新增回归和隔离下载runner。
3. `Document B2 frontend behavior and verification`：README、web/README、DESIGN、使用与部署、任务说明及本报告。

用户现在可登录正式58000入口，重点试：画布默认全屏→点Agent/题目→拖动详情宽度→查看目标/附件→继续写草稿→配置提示词/模型/MCP→历史回放和报告。无需用户执行镜像构建或服务恢复命令。真实登录后的视觉偏好与业务动作属于人工验收，本轮不通过新任务、模型调用或靶机探索验证样式。

## 7. Git发布记录

用户明确授权本次前端重构提交、合并main及推送GitHub，已按上述三组创建提交：

| 提交 | 内容 |
| --- | --- |
| `b759792` | `Expose public CTF completion requirements`：4行人类只读投影及参数化权限/隐私回归。 |
| `c0e50e4` | `Rebuild frontend with the B2 component system`：正式UI、依赖、全部浏览器适配/新增回归与隔离下载runner。 |
| `a9bc8a4` | `Document B2 frontend behavior and verification`：规范、使用文档、任务说明及实现检查点报告。 |

main与origin/main均从af1fd56出发，使用 `git merge --ff-only feature/ui-rebuild` 合入三项提交，没有冲突或历史改写；`git push origin main` 已成功发布至[GitHub仓库](https://github.com/yimingy72/blackboard-explorer)，首次远端核对main与origin/main均为a9bc8a4。后续交接进度与本节发布记录另以文档提交同步main。

此前1090普通、120集成、48前端、80浏览器的全绿快照与所提交代码一致；本次发布仅复核范围、检查日志、差异和Git状态，没有重复调用模型或重启本地服务。密钥、数据库备份、诊断/截图和web/dist构建产物不纳入本次提交。

正式源码现已位于 `/Users/yym/blackboard-explorer` 的main。保留 `/Users/yym/bbx-wt/ui-rebuild` 工作树以维护数据库恢复备份和当前本地部署路径；本次Git发布不删除运行数据、工作树或分支。最终GitHub引用以本次发布回执和 `git ls-remote origin refs/heads/main` 核对为准。

# Task-inputs-layout 补充开发报告

日期：2026-09-30。前端设计与修改由主会话完成；后端、runtime实现及独立审查由GPT-6.1 Sol high子代理协作。本轮不调用真实模型、目标或收费评测，不读写.env，不推远程。

## 完成内容

1. 模型配置标题、启用、设为默认和保存合并到同一操作行；显示名称、Provider、模型ID使用紧凑网格。Worker、MCP与创建表单移除重复解释，保留必要错误和决策信息，帮助按需展开。小屏主要控件保持44px触控区、可见焦点及无横向溢出。
2. 创建任务增加可选任务名，最多100字符。列表名称与短ID同行，只显示一次总数，搜索支持名称、目标和ID。旧任务无需回填，以目标前100字符作为展示名。完整目标仍保存并发送给Agent；工作台标题采用任务名，“任务内容”按需展开全文、原文和复制，不被长目标撑高。
3. 初始附件先上传MinIO，创建时只绑定界面明确选择的文件ID；未自动启动Agent。每文件50MiB、最多20个、总200MiB，同名文件按独立UUID保存。服务端计算size/SHA256，不接受客户端伪造路径/对象URI。上传流在DB事务外接收，有300秒时限，组锁下确认与绑定。
4. 新增migration0012：tasks.name、initial_attachments，以及一个task_input_groups表；组UUID即将来task UUID，24小时有效。Task/event/list/view及合约/OpenAPI/前端类型同步；旧任务附件为空。绑定后文件只读，下载按任务读权限返回attachment/octet-stream及nosniff。
5. 同组同规范化创建请求返回原任务，不重复创建。新UI总传input_file_ids，包括空[]；旧API省略时维持all-ready语义。上传丢响应可根据大小与实际SHA恢复已登记原件；明确移除的未知项不被残留server对象带入任务。金额哈希避免Decimal规范化舍入碰撞。
6. 启动前复用envd受控tar.zst恢复输入到/workspace/shared/inputs/<file-id>/<filename>，不自动解压/执行用户文件。三Worker初始上下文追加待核实资料清单，不自动生成Fact。续跑优先恢复已有必要归档，已准备的容器不覆盖成果。
7. 原件始终进入必要归档，即使没有Fact引用。post_fact实际读取并核对原始字节，未变复用inputs URI，改变须先复制后登记；./、//、../路径别名不得绕过原件保护。Derive/Close现有证据与看图读取支持输入URI。删除任务清理对应组、inputs和既有存档。
8. 未绑定组取消、过期、孤儿对象由后台每5分钟分批清理，失败可重试；inputs以单次PUT避免崩溃遗留multipart。取消创建页不会被延迟创建响应拉回详情；已经绑定的组受后端删除保护。组404/410可点击“重新上传附件”，保留草稿和本地文件，建立新组重传。
9. 独立浏览器复核修正5处：同名同大小同修改时间文件静默去重；冻结重试受新模型状态干扰；离页延迟导航；冻结时仍可删除验收条件；过期组缓存导致重传死路。回归包含原body不变、模型停用后原请求可重试、完整草稿保留及不同文件ID绑定。

## 实际验证

| 检查 | 结果 |
|---|---|
| make check | 877 passed，88 deselected，既有3条MCP弃用警告；ruff、pyright通过；14.88秒 |
| make test-integration | 84 passed，881 deselected；187.32秒 |
| pnpm --dir web check | ESLint、TypeScript及33项Vitest通过 |
| pnpm --dir web build | 成功；保留既有工作台大分包警告 |
| pnpm --dir web exec tsc --noEmit --project e2e/tsconfig.json | 通过 |
| Playwright全站 | 43 passed，22.9秒，包含11项本轮专项和已有设置/消息/续跑/小屏回归 |
| 独立专项复核 | 10 passed，6.1秒，仅mock API |
| Docker/MinIO输入生命周期 | 真容器恢复原件、SHA与字节一致、第二轮必要归档恢复、ZIP保持原样、旧归档兼容；集成套件包含该检查 |

可重跑命令：

```sh
cd ~/blackboard-explorer
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync
make check
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
make test-integration
pnpm --dir web check
pnpm --dir web build
pnpm --dir web exec tsc --noEmit --project e2e/tsconfig.json
PLAYWRIGHT_BROWSERS_PATH="$PWD/.data/playwright" pnpm --dir web e2e
```

日志及桌面模型/列表、手机创建截图保存在忽略目录.data/checkpoints/task-inputs-layout。浏览器测试使用模拟数据，集成测试随机端口和临时Docker/MinIO，不改变正式任务或模型配置。

## 偏差与待决

- 第一轮全量集成检查出现旧并行断连测试偶发失败：后台Derive占第五槽位，失败前未发出模型请求；晚到的种子成功也可正常解除恢复门控。仅隔离测试背景Derive并等待种子结束，仍严格核对4个Explore失败、6次模型请求、一次failure_streak与零Intent attempts。生产恢复/调度逻辑没有变化；修正后全量84项通过。
- 上传排队和文件移除支持，正在上传的单个文件暂不提供立即取消按钮；取消整个创建页会中止请求并取消未绑定组，后台清理兜底。关闭页面后本地File不会跨页面持久保存；24小时过期恢复仅适用于仍停留的创建页。
- 未登记输入对象被绑定时的选择清单排除，后台稍后清理；不承诺跨S3/PG的物理原子提交。失败清理、并发锁及活进程中取消有测试，未模拟所有进程硬崩溃/远端延迟完成组合。
- 任务名可选以兼容旧API。附件不作为已证实事实或指令，模型、Worker模板、调度、并发、Session协议、费用和现有任务续跑选择未改。
- 没有增加依赖或改uv.lock、Makefile、Compose、.env、AGENTS。共享packages/contracts新增InitialAttachment/TaskSpec字段和对应schema；objects仅增加默认行为不变的可选part_size。

## 提交划分与下一步

- daa093a：Define compact task views and owned initial attachment lifecycle（已先提交设计与任务合同）。
- 9ecb659：Add owned task inputs and idempotent named task creation：contracts、objects、blackboard、migration、合约/API与存储测试及生成schema。
- 06a2fc9：Restore and retain initial task inputs across runtime lifecycle：runtime恢复/初始上下文/证据原件/归档及回归。
- f99342b：Isolate parallel connection recovery integration fixture：只调整旧集成测试的背景调度与断言。
- 602a636：Compact configuration forms and add named tasks with attachments：前端页面、控件、手写API类型与全部浏览器回归。
- Document task inputs and verified compact interface：README、使用说明、任务补充与本报告。

按既有授权合并main并本地部署，迁移前备份；无需用户补跑沙箱外命令。后续可刷新页面新建任务并上传实际资料，用户自己的旧任务不会自动重跑。

## 本地部署

- 设计daa093a、服务9ecb659、runtime06a2fc9、测试fixture f99342b、前端602a636、说明200a925已快进合并main，未推送远程。
- 部署前再次确认active_tasks=0、pending_messages=0。先停止旧runtime，完成34MiB PostgreSQL自定义格式备份（权限600，忽略目录postgres-before-0012.dump），再重建blackboard自动迁移0011→0012，最后启动新runtime。
- 登录端点HTTP200；两服务running、restart_count=0。平台模型/工具、Worker revision10及完整配置与部署前完全一致；8任务、268会话保持，原任务历史与Session内容散列一致，没有自动续跑。
- 正式容器内创建中性输入组/文件和一个created烟测任务；同请求重试返回同UUID，任务名与附件清单正确，原件下载字节和SHA一致，bound文件删除409。仅删除烟测UUID，后台purge后Task/组404、MinIO输入前缀空。前后Agent/Session/模型账本计数相同，无start、真实模型或目标调用。烟测记录为lifecycle-smoke.log。
- 最终再次核对active_tasks=0、pending_messages=0、platform_versions=3、schema0012。blackboard与runtime中96个生产文件的SHA256均与main一致，前端index SHA一致。
- 镜像为bbx-blackboard:latest/bbx-task-inputs-blackboard:latest（4e44209d211b），bbx-agent-runtime:latest/bbx-task-inputs-agent-runtime:latest（c27b02a6a17f）；静态资源、最终日志、截图及不含凭据的部署元数据保存在主仓库.data/checkpoints/task-inputs-layout。
- 刷新http://127.0.0.1:58000即可使用。无需用户执行额外部署命令；备份含真实任务/会话内容，应仅作为本地恢复资料保存，不提交或分享。

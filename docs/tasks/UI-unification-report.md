# 正式工作台替换与前端风格统一报告

日期：2026-10-09。开发目录：`/Users/yym/bbx-wt/ctf-team-mode`。本轮沿用已有工作树，保留之前所有未提交改动，没有提交、推送或合并。

> 发布补充（2026-10-10）：用户已授权提交、合并与推送。截图、原型和本机诊断产物仅在本地留存，不随功能源码发布；测试可以重新生成截图。用户同时要求只保留当前镜像，本文记录的旧回退标签已清理，最新部署与发布结果见CTF任务报告后续章节。

## 1. 完成内容

- 正式替换用户选定的C方案：默认完整画布，点击Agent进入连续会话，点击题目进入详情。侧栏支持鼠标拖动、左右方向键、Home/End、双击复位、关闭与Esc。手机使用工作区内的全宽详情，关闭后返回画布。
- 压缩任务头部：任务名、状态、时长和费用集中展示；长目标与附件按需查看。修复CTF state覆盖详情导致的币种字段丢失，真实任务正确显示人民币。
- 题目详情分为概览、记录、文件。来源明确的候选/最新记录、验证状态、参与Agent与关键证据优先展示；全部候选可展开，记录保持分页，文件计数基于全量相关记录。没有生成新摘要、精确flag字段或虚构TODO。
- 按用户后续要求删除人工确认、填写验证结果和手动调整验证要求的入口及前端API调用。已有历史验证仍只读显示，不删除旧记录或将候选改成通过；后端兼容接口未在本次前端改版中移除。
- 选Agent直接看对话，不默认显示公开活动。展示服务返回的推理文本/摘要、模型正文、工具参数和配对结果。纯推理消息保留；不透明回放字段不显示。支持已执行工具先保存在 `bbx_tool_results`、尚未进入正常消息历史的情况，正常历史结果优先，坏数据保持等待状态。
- 统一SVG动作图标、按钮/导航/输入反馈、折叠箭头、面板与原生弹窗过渡。共用160ms快速反馈、200ms面板过渡与同一缓动曲线，减少动态偏好关闭这些效果。未新增字体、图标包、动画依赖或主题体系。
- 统一Ctrl/⌘+Enter发送快捷键、忙碌/禁用反馈、原生dialog、Esc和焦点恢复。修复旧Fact/Intent详情关闭后焦点丢失；新侧栏关闭不会抢走用户已经转向菜单或其他控件的焦点。
- 图谱保存用户缩放、拖动与位置，轮询或选中变化不重置视角；未被用户操作的画布在侧栏改变尺寸后适配。保留ReactFlow测量状态，避免手机关闭侧栏后节点消失。
- ELK布局引擎改为普通黑板需要布局时才加载；CTF工作台自身脚本从1774.01KB降至319.83KB，gzip从546.31KB降至104.32KB，约减少82%。这不包含公共脚本，也不代表模型求解速度提高；ELK独立块仍有较大包警告。

## 2. 平台审查与验证

审查范围覆盖登录、任务列表、创建任务、Worker/CTF配置、模型/MCP配置、普通黑板工作台、CTF工作台、Agent对话、证据/工作区、复盘与报告。截图为完全Mock数据，不含真实凭据或推理原文。

| 检查 | 实际结果 |
| --- | --- |
| `make check` | 1086 passed / 124 deselected，21.43秒；ruff、pyright通过，3条既有MCP弃用警告 |
| `make test-integration` | 120 passed / 1090 deselected，286.80秒 |
| `pnpm --dir web check` | ESLint、TypeScript及48项单元测试通过 |
| `pnpm --dir web build` | 通过；ELK独立大块警告保留 |
| 全套Playwright | 70 passed，约1.3分钟，复用本机Chrome，无新下载 |
| E2E TypeScript | `tsc --project e2e/tsconfig.json --noEmit`通过 |
| 界面截图 | 42张，覆盖1440、768、390px，主要页面已视觉复核 |
| 真实本地页面只读检查 | 正常登录；默认侧栏关闭；Lead30条、web-runner11条推理正常显示；旧失败任务两条checkpoint工具结果显示完成；币种与人工入口删除均验证；任务写请求0、页面JS错误0 |

浏览器回归保留发送幂等与重试ID、成员停止/恢复、任务续跑/删除隔离等原业务断言，新增拉伸、焦点、视角保存、推理/工具展示、人工入口移除、历史人工记录只读及系统事件失败重试。测试曾找出手机图谱与快速关闭后菜单抢焦点问题，已修复，没有添加等待或弱化断言掩盖。

真实页面检查只读取已有任务和Session，没有发送消息、执行命令、续跑靶场或调用模型。保存的模型内容仍按完整响应checkpoint刷新，未声称实现逐token推理流。

截图目录：`docs/tasks/ui-unification-screenshots/`。完整构建、检查、部署与只读记录位于忽略目录 `.data/checkpoints/ui-unification/`。

## 3. 部署时发现的配置初始化问题

第一次网页服务启动时，旧初始化逻辑因文件模板与平台保存值不同，自动追加CTF默认配置v4。任务、Session、模型及历史散列均未变化，只有profile集合发生变化；这不应成为前端更新的副作用。

- ProfileStore现仅在同名配置不存在时初始化，检查与创建共用事务锁。已有平台配置不因重新启动或默认文件变化而被覆盖；显式发布、同内容去重、过期版本409行为保留。新增用户/system编辑保留及8路并发首次初始化回归，连同现有UI保存/默认模型/任务快照流程均通过。
- 对本次自动生成的未使用v4，核对它是唯一profile变化、无任务引用且无活动任务，备份后仅清理该新行。没有修改或删除任何原版本；原profile集合散列恢复一致。该清理不是通用回滚命令，不需用户执行。
- 重建并再次定向替换blackboard后，原9个任务、13份Session、完整profile/model/任务历史散列与部署前逐一相同，schema仍0014、活动任务0。runtime未重启，镜像仍 `a81e51fb23a5`。

正式网页镜像：`bbx-ctf-blackboard:ui-20261009`，配置ID `e2709030a15a`；专用CTF compose固定使用同项目前缀tag，避免修改其他项目的镜像引用。服务running/restart0，11个网页产物散列与构建输出匹配。第一次产物核对发现构建时序导致旧文件被复制，未用该不匹配产物交付，最终重建后核对通过。先前镜像回退标签为 `bbx-ctf-blackboard:before-ui-20261009`。

## 4. 复现命令

以下检查不调用真实模型：

```sh
cd ~/bbx-wt/ctf-team-mode
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
pnpm --dir web check
pnpm --dir web build
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  pnpm --dir web exec playwright test --workers=1
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal' \
  make test-integration
```

本地页面：`http://127.0.0.1:58000/tasks/8512ace2-c359-4021-8625-7be162cbeef1`。无需用户重复测试或部署。

后续改源码重新部署时，先通过检查及完成web build，再构建 `services/blackboard/Dockerfile` 为 `bbx-ctf-blackboard:latest`，确认无活动任务后使用 `scripts/deploy_ctf_team_mode.sh up -d --no-deps --force-recreate --wait blackboard`。正常Compose入口消费现有配置，本轮没有读取或修改.env。

## 5. 偏差与待决

- 这次前端更新包含一项必要的后端初始化修复，原因和数据核对见第3节；没有修改runtime、依赖、数据库迁移或docs/design。
- 本轮未重跑模型求解或整套Cybench，未验证Safari及真实手机设备；动画支持以本机Chrome和减少动态回归为证据。
- 此前test3的预算准入空转问题未在本轮修改，不能把UI显示修复视为调度修复。

## 6. 建议提交划分

先审查既有CTF基础改动，再独立保留本轮变更：

1. `Expose saved reasoning and tool results in agent conversations`：view/types及其单元回归。
2. `Unify canvas workbench and interface interactions`：工作台、题目详情、共享图标/过渡、焦点与ELK按需加载、compose镜像前缀及web/DESIGN。
3. `Preserve platform profiles across service restarts`：ProfileStore与普通/真实PG回归。
4. `Refresh frontend regression coverage`：e2e夹具、行为检查、截图和报告。

没有自动暂存、提交、推送或合并，其他既有未提交工作保持。

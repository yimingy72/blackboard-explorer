# UI-detail-review · 使用细节复核与修正报告

日期：2026-10-10至2026-10-11。工作树 `/Users/yym/bbx-wt/ui-rebuild`，分支 `feature/ui-detail-review`，基于已发布main `0115a19`。沿用用户确认的B2，主会话负责界面设计与实现，子代理统一GPT‑6.1 Sol high。

## 1. 审查结果与已完成修改

本轮从负责人“查找任务→进入详情→阅读Agent→查看报告→返回列表”的路径审查，先独立视觉/代码评估，再静态扫描；没有把组件lint或截图可见当作设计质量结论。独立初评为25/40（主观Nielsen评分），主要问题是导航跳位、字段误导和阅读宽度。

| 问题 | 修正及实际行为 |
| --- | --- |
| 进入任务插入“协作画布”，配置菜单跳位 | 全局导航固定任务/Agent配置，详情归属任务；侧栏位置保持。 |
| “查看”用href整页刷新，与任务名称入口不一致 | 内部入口统一Router导航，保留框架和展开的侧栏；空态创建同样不整页重载。 |
| 列表CTF验收固定“按题协作”，finished绿色“已完成”误导 | 删除验收列，模式独立；两模式任务终态统一“已结束”中性色，不把执行结束当作验收通过。真实验收/候选记录保持。 |
| 空usage金额默认零，缺耗时，手机只能横滚 | 未知费用“—”，真实零仍显示零；显示已记录费用、预算和累计运行耗时，创建日期按断点渐进显示。手机纵向记录直接呈现全部信息及操作。 |
| 搜索与页码在返回详情后丢失 | q/page保存在URL，详情与报告携带受约束的返回位置；返回同一查找页面。 |
| 440px详情中的证据和工具内容压成163–225px窄条 | Bubble助手记录撑满可用宽度，EvidenceViewer宽度明确；ThoughtChain用公开item样式安排图标与可增长正文，两模式结果块在440px面板均大于300px。 |
| 首次选CTF节点及容器变宽触发fit，新增关系移动已拖节点 | 移除容器resize自动fit，节点选择标记用户操作；保留已有坐标，仅布置新增节点。明确缩放/适配入口保持。 |
| 899px断点卸载会话，丢阅读及展开状态 | 会话保持同一个DOM挂载位置；桌面用成熟Panel预留/调宽，窄屏覆盖工作区，隐藏仅改变布局。工具展开、证据滚动、草稿与请求ID保持。覆盖画布inert，不进入不可见的键盘焦点。 |
| 宽度动画裁字、按钮长波纹、终态报告不突出 | 侧栏宽度即时切换，关闭装饰波纹，保留控件状态反馈和短时详情淡入；报告显式显示，续跑为次级动作。辅助字号统一修正到12px，记录时间采用中文24小时格式。 |

拖动复核暴露并修复了新布局约束问题：每一步onResize若同时改变defaultSize，会使组件重注册并只接受首步拖动。默认宽度只在详情开闭/断点边界取值，拖动回调稳定；保留原140px拖动及宽度阈值、键盘End/方向键和双击复位验收。

保留现有登录、配置、Worker角色、模型/MCP、创建附件、定向消息、会话幂等/重试、停止/恢复/删除、Goal/证据、裁定/回放、归档/报告等行为，没有新框架、依赖、数据库、API或领域规则变更。

## 2. 实际验证

| 检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check` | Ruff247文件通过，Pyright0错误/警告，1090 passed /124 deselected，25.97秒；3条既有MCP弃用警告。日志 `.data/ui-detail-review/backend-check.log`。 |
| `pnpm --dir web check` | ESLint/TypeScript通过；15文件、52单元测试通过。新增关系坐标保持、返回位置边界和时间格式回归。日志 `frontend-final.log`。 |
| `pnpm --dir web build` | 通过，28.59秒；仍保留Vite大于500kB chunk提示。日志 `build.log`。 |
| `antd --version 6.6.5 --format json lint web/src` | 0 issue，使用公开组件/语义API。日志 `antd-lint.json`。 |
| `BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' pnpm --dir web e2e --workers=3 --reporter=line` | 最终隔离版89 passed，2.2分钟，exit0；原80及新增9项。日志 `e2e-final-isolated.log`。 |
| E2E `tsc -p e2e/tsconfig.json` / `eslint e2e --config e2e/eslint.config.mjs` | 都通过；日志 `e2e-types.log`、`e2e-lint.log`。 |
| Impeccable静态扫描 | 返回空发现数组；TSX/库生成布局仍需人工和浏览器核对，不据此称设计完美。没有在真实用户页面注入检测脚本或叠加层。 |
| 发布核对 | 12个文件经实际HTTP读取与构建SHA256逐一一致，登录入口HTTP200；API/runtime容器ID与启动时间保持。 |

浏览器覆盖1440、1200、899/900、768、650、390px及原有短屏用例。验证导航不跳、无document请求、URL查询往返、未知/零费用、手机状态和操作可见、节点及视角保持、宽度拖动、同一会话DOM、工具正文宽度、阅读scrollTop120、草稿和inert键盘隔离。原业务/隐私/正文/几何断言保留；旧“打开面板后自动让全部节点进入缩窄画布”的预期改为首次适配及之后保持视角，与设计要求一致。

完整回归曾发现Mock全局created事件模板被旧长目标测试修改，污染同worker后续用例。已在夹具入口structuredClone隔离模板，并在同worker复验污染链路，再跑全部89项；不是生产任务数据变更，未降低标题断言或扩大超时。

实际查看了修改前后桌面列表、390px记录、普通/CTF会话工具块、配置与创建页面截图。所有任务、模型、下载与事件验证均使用完整Mock及隔离随机端口服务，没有58000业务回退、真实模型调用或真实任务操作。

组件参考：[Bubble公开样式与角色配置](https://x.ant.design/components/bubble-cn/)、[ThoughtChain公开语义布局](https://x.ant.design/components/thought-chain-cn/)。未覆盖内部.ant-*选择器。

## 3. 本地发布

入口保持 <http://127.0.0.1:58000/login>，刷新并使用原账号登录。先复制新hash资源、保留旧hash供已打开标签使用，最后原子替换index；没有重启API/runtime或修改任务数据、配置、凭据及.env。

index SHA256：`1e3d06c266d1ebcf5cdcb68d4a8bc3e3fe8882b2353342426e3f8daf6ad3b639`。12文件清单 `.data/ui-detail-review/deployed-assets.json`。

为后续重建打包仅更新web/dist的本地镜像，network=none，当前 `bbx-ctf-blackboard:latest` 为 `b1ba40a2448622d7f4556b61e6c781a70c78a33dc60a779de211ee1a89fffc20`。没有新增回退标签，也没有操作其他Docker项目。

API容器 `a92f2c92`，启动于2026-10-10T14:22:31Z；runtime `806924a1`，启动于14:23:18Z；前后restartCount均0。本次是静态发布，不要求用户再次构建或恢复环境。

## 4. 偏差、未覆盖边界与下一步

- 本轮前端局部修正，没有更改docs/design正本、后端、OpenAPI、调度、模型费用计算、验证或提示词；产品规范web/DESIGN与web/README已同步导航/字段/会话行为。
- 费用只展示GET/tasks已提供的任务账本，CTF复盘费用不在该字段内；没有虚构CTF验证进度，也无法从缺价格告警的列表判断模型是否免费。
- 未重跑Docker集成；本轮没有后端变更，既有隔离下载/事件/生命周期浏览器业务回归全部保留。没有真实设备、Firefox/Safari或生产性能测量，不承诺所有主观设计细节已完善。
- 正式用户登录后的真实内容仍需人工验收；没有读取.env取得账号或绕过认证，样式截图使用合成数据。
- 修改保留在feature/ui-detail-review，未提交/合并/推送。本轮建议按“导航和列表语义”“会话阅读/动效/画布稳定”“回归与使用规范”分组；需要后续Git发布时再处理。

可重点验收：带搜索/分页进入和返回任务、手机列表、首次点击Agent后的视角、拖宽详情、打开工具结果后跨断点、终态查看报告，以及快速切换Agent时的动效。

## 5. 第二轮补充审核（2026-10-11）

用户要求继续审核其他前端细节，本轮沿用 B2，集中检查配置、新建任务、附件、历史工作区和报告。主会话修改 UI；三个 GPT-6.1 Sol high 子代理分别复核表单、阅读路径和浏览器回归。

| 发现的问题 | 已完成的修正 |
| --- | --- |
| 配置页只保护关闭页面，点击内部导航或浏览器返回可能丢失草稿 | 使用已有 React Router 的 data router / `useBlocker`，确认后才离开；保存期间不能放弃写入，成功且没有其他草稿后继续已请求的导航。登录过期跳转保持。 |
| 保存后继续编辑，仍显示旧“已保存”提示 | Worker、模型、MCP、CTF 再次编辑时清除过期成功提示，保存按钮与未保存状态保持一致。 |
| 新模型的必填价格藏在折叠区，错误后不知道改哪里 | 新建时展开价格；验证失败展开、滚动并聚焦无效输入。零费率需要显式填 0，不把空值当作免费。 |
| 编辑旧非人民币配置直接沿用数值并改成 CNY | 不进行隐式换币；展示原币种及单价作为参考，要求重新填写人民币单价。 |
| MCP 修改连接后仍能读取旧服务版本工具，看起来像在测试新配置 | 修改立即清除旧工具结果，保存后才允许读取对应版本工具；忽略过期请求结果。 |
| CTF 取消工具仍携带孤立的用途映射，保存可能返回 422 | 仅提交当前角色草稿与另一角色已保存工具仍引用的映射；共享用途保留，另一角色未保存的新用途留在草稿内，不提前发布也不丢失。 |
| 新建任务缺模型引导离开当前页面，目标及附件丢失，且打开的是 Worker 页 | 新标签页直接进入模型配置；原表单保留目标与附件，回到窗口或点击“刷新模型”读取可用模型。 |
| 任务详情初始附件下载错把 task ID 当成上传组 ID | 根据附件真实 URI 生成下载链接，隔离测试验证实际下载字节。 |
| 历史工作区树、预览和下载默认为最新轮次 | 请求、缓存键和下载链接均携带选中的 run，切换轮次不会混读同名文件。 |
| 报告元信息失败一直显示正在读取；重新登录丢失历史轮次 | 失败时展示短任务 ID 与独立重试，报告正文保持可见；登录往返保留查询参数。 |
| 模型/MCP 初始加载短暂显示空态；保存图标干扰按钮可访问名称 | 加载使用静态 Skeleton，空态和错误分开；主要提交按钮使用稳定名称和 `aria-busy`。 |

没有引入依赖。Router 调整使用现有 6.30.1 的公开 API，以覆盖 SPA 和历史返回；关闭页面仍由既有 beforeunload 处理。参考 [React Router useBlocker](https://reactrouter.com/6.30.1/hooks/use-blocker)。组件改动使用公开 API，不覆盖 `.ant-*` 内部结构。

### 第二轮检查与边界

- `pnpm --dir web check`：ESLint、TypeScript 全部通过，15 文件 / 52 单元测试通过。
- `pnpm --dir web build`：最终通过，15.45 秒；保留已有大 chunk 提示，没有把它当作性能已优化的证据。
- Ant Design CLI：0 issue。`git diff --check` 通过。
- 新增 16 项隔离 Mock 浏览器用例：表单 10、CTF 工具绑定 2、附件/历史/报告 4。原回归的业务、隐私、尺寸和拖动断言保留。
- 最终全套 `BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' pnpm --dir web e2e --workers=3 --reporter=line`：**105 passed，2.6 分钟，exit 0**；Chrome、完整 Mock、隔离随机端口，没有生产 API 回退。E2E TypeScript 与全目录 ESLint 均通过。最终日志 `.data/ui-review2/e2e-final.log`、`e2e-types.log`、`e2e-lint.log`。
- 已实际查看 390px 价格校验与缺模型提示、1440px MCP 修改状态、初始附件、历史工作区和报告错误截图。截图与日志放在忽略的 `.data/ui-review2/`，部分表单截图在 `.data/ui-detail-review/forms/`。
- 前两遍完整回归均为 104/105，先后发现保存、读取 MCP 工具的加载图标改变了按钮可访问名称，影响重复操作。修复生产按钮，并统一启动/停止按钮的同类状态；保留精确名称和忙碌状态断言，没有放松选择器来掩盖问题。原失败日志保留在 `e2e-before-button-label.log`、`e2e-before-inspect-label.log`，两项专项复验均通过。
- 本补充仅改前端；没有再次运行 Python 普通检查或 Docker 集成，上一轮的 1090 项 Python 结果仍属上一轮。没有真实模型调用、真实任务或配置写入、真实设备与 Firefox/Safari 验证。正式登录后的真实内容仍需用户验收。

建议新增一组提交 `fix(web): preserve configuration drafts and historical resources`，包含本轮配置/表单/历史内容修正、三个补充 E2E 文件及文档；与第一轮的导航、列表和会话修正一并保留在 `feature/ui-detail-review`，尚未提交或合并。

### 第二轮本地发布

已更新 <http://127.0.0.1:58000/login> 的正式静态资源。12 个构建文件逐一通过实际 HTTP 内容 SHA256 核对，登录入口返回 200。当前 index SHA256 为 `4a704f801b3fde1e4346acd75ea2c0ee2453de13d81c31a1918e3d9fc6f7f9b8`；核对清单 `.data/ui-review2/deployed-assets.json`。

只复制静态文件并原子替换 index，旧 hash 资源保留供已打开标签使用。API `a92f2c92` 与 runtime `806924a1` 的容器 ID、启动时间和 restartCount 前后完全一致；没有重启容器、修改任务数据、配置或 .env。另以 `--network=none` 打包当前静态文件，本地 `bbx-ctf-blackboard:latest` 更新为 `1be2948d9cef55455fc9a89bdb1d33d8ea1ec494e44c33ceb97c66f4680d6610`。

本轮重点验收：修改配置后返回任务列表、缺少模型时保留创建草稿、空价格校验定位、MCP 修改后保存再读工具、CTF 取消工具保存、初始附件下载、旧轮次文件及报告失败重试。

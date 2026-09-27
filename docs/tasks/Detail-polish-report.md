# Detail-polish · 详情与画布精简报告

日期：2026-09-27。设计先行提交：`2022d61`。

## 完成内容

- 右下角缩略图由 React Flow 默认尺寸缩为 128×84，保留导航与缩放操作。
- 事实、意图和目标详情改为简短对象标题与 Markdown 正文；支持段落、列表、行内代码、可独立滚动的围栏代码块。意图预期、方法、交接笔记与任务背景使用同一排版，不再把完整陈述作为粗体大标题。
- 删除事实类别/状态、意图状态、Agent、验收项筛选及关闭分支折叠选项，并删除对应的过滤逻辑；当前实时或回放版本的所有对象完整展示。验收摘要与运行统计保留。
- 使用已有 react-markdown，不新增依赖。HTML 作为文本显示，不执行脚本；正文中的图片不自动请求外部地址。

## 实际验证

- `make check`：格式、lint、pyright 通过；380 项普通测试通过，无真实模型调用。
- `make web-check` / `make web-build`：31 项前端测试通过，生产构建通过。测试数减少 2 项是因为删除了已移除筛选功能的测试。
- Playwright：10 项通过；覆盖 Markdown/代码块、原始 XML 文本、脚本不执行、图片不加载、128×84 缩略图、所有筛选移除、302 对象完整图谱、390px 无横向溢出，以及已有对话、回放、表单、报告与归档流程。
- `make test-integration`：38 项 Docker 集成测试通过。blackboard 镜像已构建，`git diff --check` 和浏览器测试 TypeScript 检查通过。
- 已人工查看桌面与移动截图，日志与截图保存在主仓库忽略目录 `.data/checkpoints/detail-polish/`。没有读取或修改 `.env`，没有调用 DeepSeek。
- 本地 blackboard 前端服务已更新，HTTP 页面与最终构建一致；未重启 runtime，既有任务与数据保留。

## 偏差与待决

没有改变黑板数据与后端行为，也不改写原有事实陈述。原文里的 Markdown 按其实际格式呈现；普通文字不会被自动改写成推测出的列表或代码。原先超过 300 个对象的自动折叠同时取消，避免用户没有筛选控件后仍看到不完整的图谱。

## 提交与交付

1. `Define readable details and an unfiltered compact canvas`：设计与任务说明。
2. `Polish markdown details and simplify canvas controls`：前端、浏览器验证及文档。

合并后更新本地 blackboard 前端服务并同步 GitHub。刷新即可使用，不需新建任务或补跑检查。

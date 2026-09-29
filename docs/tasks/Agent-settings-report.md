# Agent-settings 报告

## 完成内容

主会话独立审查并实现前端，后端委派 GPT-6 Sol high 实现后由主会话复核。

- 「平台模型」更名为「模型配置」。Worker、模型与MCP统一采用侧栏选择、主区域编辑的布局；清理旧历史版本/对比的失效样式。
- 修复原Worker标题h2没有对应样式（旧样式只匹配h3）造成的字号/间距失控。中文角色名优先，英文辅助；统一标题、标签、控件高度、段落及表单间距。提示词使用独立等宽阅读区域，工具区分组；保留草稿保护、保存反馈与并发编辑冲突处理。
- 模型/MCP表单按连接、上下文与能力、凭据、计费分组。角色页签支持方向键/Home/End与焦点管理，手机视口改为纵向表单，无横向溢出。
- 模型新增 `context_window`，严格正整数或null。新任务模型容量显式配置时，`context_threshold=max(1, context_window*4//5)`，多角色原生Profile取最小显式容量；任务参数和Profile快照一致。未配置保持旧参数合并规则；旧任务及续跑保留原阈值。示例500000对应400000。
- 页面显示换算和生效范围；该设置不扩大服务商真实容量。默认60次探索/20次种子调用上限未改变。没有改提示词正文或现有模型配置。

## 验证

实际运行：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync
make check
.venv/bin/pytest -m 'integration and not live'
pnpm --dir web check
pnpm --dir web build
PLAYWRIGHT_BROWSERS_PATH=/Users/yym/blackboard-explorer/.data/playwright pnpm --dir web e2e --workers=2
```

- `make check`：ruff、pyright全绿，586普通测试通过。
- 集成：65通过。使用与`make test-integration`相同的pytest选择，复用现有依赖镜像，未执行该Make目标的全量latest镜像覆盖构建。
- 前端：33单元测试、ESLint、TypeScript和生产构建通过；20浏览器测试通过。最后对控件等高调整后重跑2个配置页专项测试通过。
- 上下文覆盖保存/回显/清除、拒绝小数/bool/0、默认模型到新任务400000阈值、原生多角色最小容量和旧参数兼容。
- 已检查1440px桌面与390px手机截图，包括Worker、模型、MCP。截图位于工作树 `.data/checkpoints/agent-settings/`（忽略文件）。
- 构建 `bbx-agent-settings-blackboard:latest` 与 `bbx-agent-settings-runtime:latest` 成功；Runtime镜像可加载新增字段。
- 无真实模型调用，没有读取或修改.env。前端构建仍提示原有工作台大chunk，非本页新增问题。

## 共享文件与偏差

变更contracts模型/关联JSON schemas、OpenAPI与生成TypeScript类型；没有新增依赖或数据库迁移。关联schema导出同时补齐已实现的图片输入和峰谷计费字段，避免继续保留陈旧快照。

20%是本次明确的预留规则，并非厂商API参数，也不保证超长工具输出不会超限。当前统一任务阈值继续用于Explore交接与Derive下一轮分段，没有引入新的压缩服务。

## 提交划分

1. `Define model context capacity and agent settings layout`：设计与任务合同。
2. `Support model context capacity in task snapshots`：contracts/API/快照与后端测试、生成类型。
3. `Redesign agent settings and model configuration forms`：前端布局、上下文表单、浏览器测试、使用文档与本报告。

## 本地交付

部署前确认无活动任务，再切换构建后的blackboard/runtime镜像；部署结果另附下方。

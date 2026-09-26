# Workbench-UX · 工作台与 Agent 对话

依据：实现架构 3/4/7/8，M4-report、Docs-current-report。用户要求 GPT-6 Sol（high）实施，由主代理设计和验收。

## 范围与完成标准

1. 核对 Agent 编号按任务隔离，补充跨任务测试，所有界面使用一致的任务内展示编号，保留后端引用 ID。
2. Agent 常驻选择条与拓扑联动；右侧对话展示实际记录的初始上下文、黑板注入、模型输出/可用推理字段、工具调用，支持旧任务、空态、失败、历史回放。
3. 压缩验收、状态说明与过滤区，扩大拓扑；显示实时/终态/回放任务时长。
4. 重做创建表单：目标与背景组成主编辑区，验收条件清楚可编辑，预算与 Profile 为次级设置；统一控件高度、间距、可访问性与窄屏布局。
5. 不改探索语义，不引入新 UI 依赖，不读写 `.env`。使用假模型验证；通过 make check、web-check/build、集成与浏览器检查后部署。

## 分工

- 后端：services/blackboard、services/agent-runtime、packages/contracts、OpenAPI 与相关测试。
- 工作台：TaskWorkbenchPage、Agent 对话组件、board 显示辅助、事件订阅与对应前端测试。
- 创建表单：NewTaskPage、其 CSS，以及必要的共享表单控件样式；不修改工作台文件。
- 主代理：设计、任务文档、跨组件整合、验收与部署。子代理不执行 Git 提交或修改设计。

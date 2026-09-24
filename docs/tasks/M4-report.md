# M4 · 工作台补全报告

完成日期：2026-09-25。工作分支：`m4`，起点 `abea236`。设计先行提交：`af29bb3`。

## 1. 完成内容

| 编号 | 实现 |
|---|---|
| U.1 | 事实详情展示证据、争议、后续引用、验收关系；意图展示交接 notes、attempts、retry、结论事实；Agent 展示模型调用、上下文、结束/收尾原因、回执、工具调用及费用。详情只读取所选版本的投影。 |
| U.2 | 六类证据渲染：HTTP 请求/响应、日志高亮、带行号代码、脚本/输出、命令/输出、文本。按需获取；超过 200 KiB 保留头尾，提供原文下载，二进制不渲染为文本。兼容 MAF 工具结果中的嵌套 JSON。 |
| U.3 | 验收状态、最近裁定/缺口、金额预算、缓存命中率、模型调用次数与结束说明；历史模式禁用启动/停止。小于 0.001 的正费用显示 `<0.001`，避免误显为零。 |
| U.4 | 抽屉含 Agent 表、事件流、裁定历史、时间轴和工作区；键盘方向键切换页签。事件分批显示，展开才序列化载荷。回放仍接收实时事件，但图谱、详情、费用与回执以历史版本为准。 |
| U.5 | Markdown 报告页，事实/意图引用跳转工作台，完整 evidence/toolcalls URI 转下载链接；禁用原始 HTML，不自动请求报告中的图片。 |
| U.6 | Profile 名称/版本列表、完整 YAML 编辑校验、差异、发布旧内容为新版本、本浏览器固定默认版本；保留 prompt_templates 正文。新建任务说明全局出网白名单的实际边界。 |
| U.7 | 按事实类别/状态、意图状态、Agent、验收项过滤；验收过滤包含裁定直接引用的事实及其来源。超过 300 对象默认折叠已关闭分支，保留仍开放或争议中的分支，可一键恢复。 |
| U.8 | 增加只读归档目录/文件 API、访问隔离、受限索引缓存、安全路径校验和普通/真实 MinIO 集成测试；同步 OpenAPI 与 TypeScript 类型。 |

## 2. 接口与归档限制

- `GET /api/tasks/{id}/workspace/tree`：返回 `{entries: [{path, kind, size}]}`。
- `GET /api/tasks/{id}/workspace/file?path=...`：返回 `{path, size, text, truncated, binary}`。
- 两者复用任务读取鉴权，只访问任务已登记的 `workspace/{id}.tar.zst`；没有归档返回 404，非法路径/链接预览拒绝。
- 索引缓存最多 2 个归档；压缩数据上限 2 GiB，解压上限 512 MiB，tar 成员最多 50,000，zstd 窗口最多 64 MiB。超过预览限制仍可通过已有下载接口获取原归档。
- 使用临时压缩文件/解压 tar 建索引，不解包到宿主目录；只读取普通文件，软/硬链接只列出不跟随，设备/FIFO 不展示。拒绝路径穿越、绝对路径与冲突条目。锁保护并发读取与 LRU 淘汰，服务关闭释放缓存。

## 3. 实际验证

| 检查 | 结果 |
|---|---|
| `make check` | Ruff、Pyright 通过；309 个普通测试通过，36 项集成/live 测试排除；没有真实模型调用。 |
| `make web-check` | ESLint、TypeScript 通过；29 个前端测试通过。 |
| `make test-integration` | 镜像构建成功；32 个集成测试通过。新增用例使用真实 PostgreSQL/MinIO，验证归档读取、同任务访问、异任务 403、未认证 401、链接不跟随。测试占位 JWT 密钥长度修正后，新增用例定向复跑通过且无警告。 |
| `make web-e2e` | 6 项 Playwright 冒烟通过：登录、六类证据、回放隔离/初始费用、Agent/裁定、工作区/报告、Profile 发布/默认、302 对象折叠/390px 页面宽度。该命令独立于 `make check`。 |
| `make image-blackboard` | 前端生产构建与 blackboard 镜像构建通过。Vite 对 ELK 所在工作台 chunk 提示体积较大；工作台已按路由懒加载。 |
| 真实运行 UI 复盘 | 使用 M3b 真实 DeepSeek 任务和已有数据库/MinIO，浏览器检查通过；M4 没有新增模型费用。 |

浏览器复盘任务：`25ca10e4-8e1e-46cf-8901-82e165f0ea6c`。

- 顶栏明确因 2/2 验收全部满足而结束；账本总额约 USD 0.070261，缓存命中率 89.9%，43 次模型调用。
- Agent 表列出 4 个 Agent 的类型、19/9/8/7 次调用、正常结束与各自费用；种子详情可查看完整结束回执与 24 条工具调用。
- 报告 F2 链接可定位根因事实，代码证据可展开原文；归档树 `shared/order-service/orders.py` 可预览 8,510 字节文本。
- 回放到 v50 显示 0/2 验收、1 个 Agent、1 个事实；未来 F2 不可见，结束报告/归档入口消失，控制按钮只读。返回实时恢复现状。
- 1440×1000 与 390×844 视口检查；配置页显示完整版本快照。Profile 发布/差异/默认写入由隔离 Playwright 数据验证。
- 测试基础设施使用随机端口与独立容器；保留的 M3b 复盘容器在 M4 完成后清理，原报告/归档继续保留在主工作树 `.data/checkpoints/m3b/`。

复跑命令（不需要读取 `.env`，不调用 DeepSeek）：

```sh
cd ~/blackboard-explorer
export UV_CACHE_DIR=/private/tmp/bbx-uv-cache
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
export all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal
uv sync --locked
pnpm --dir web install --frozen-lockfile --store-dir /private/tmp/bbx-pnpm-store
make check web-check web-e2e
make test-integration DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal,mirrors.aliyun.com'
```

Playwright 首次下载由 `PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright` 完成，仅安装 Chromium headless shell，缓存位于工作树 `.data/playwright`。这不是模型下载。

## 4. 偏差与待决

1. 用户已授权后续设计/Git/Docker/浏览器操作，因此本轮自行运行任务说明原定由用户运行的检查。没有修改或展示 `.env`，没有调用真实模型。
2. “设为默认”采用本浏览器 `{name, version}` 偏好；已有后端只读/版本发布 API 不具备全局默认写接口。已先同步设计，现有任务仍使用固定快照。回滚保留所有旧版本。
3. 归档预览采用固定资源上限，超限可下载原件。未引入任意文件写入、解压路径或链接跟随机制。
4. 现有终止事件只携带 `terminated`，没有细分时间耗尽和连续推导空结果。界面如实提示这两个可能原因，不伪造精确原因；`accepted`、人工停止和失败原因可明确展示。进一步细分属于调度事件契约变更。
5. 金额沿用 Profile 的计价单位；默认 Profile 为 USD，界面显示账本估算，不声称与供应商实付一致。
6. 初次 Playwright 运行因测试路由误拦截 Vite `/src/api/client.ts` 而空白，已将 fixture 拦截限定到 `/api/`；最终全套通过。审查另修复历史费用回退、验收引用过滤和省略段含 NUL 的二进制识别。
7. 当前未收到 EVAL-2 已合并的确认；M5 在依赖满足之前不启动，不代做 EVAL-2。

## 5. 共享文件与依赖

- `Makefile`：新增 `web-e2e-install` / `web-e2e` 和国内浏览器镜像、任务目录缓存默认值。
- `.gitignore`：忽略 Playwright 结果/报告；不纳入截图、浏览器二进制或密钥。
- `services/blackboard/pyproject.toml` / `uv.lock`：将已锁定的 zstandard 声明为 blackboard 运行依赖，用于 tar.zst 只读预览。
- `web/package.json` / `pnpm-lock.yaml`：新增 react-markdown、yaml、开发期 Playwright，均为任务明确需要。
- `services/blackboard/openapi.json` 与前端 schema：同步两项只读接口。未修改 contracts、Compose、全局配置。

## 6. 提交划分与下一步

1. `Define workbench defaults and safe archive previews`：设计同步，已提交 `af29bb3`。
2. `Add bounded read-only workspace archive APIs`：归档后端、接口/集成测试、OpenAPI、Python 运行依赖和锁文件。
3. `Complete traceable exploration workbench`：全部前端、Playwright、Makefile、忽略规则和本报告。

提交后合并 main，打 `m4`，清理任务工作树/分支并更新 HANDOFF。M5 仍需用户确认 EVAL-2 已合并；本轮没有必须由用户补跑的检查。

# CTF 明亮环绕协作 · 正式替换报告

日期：2026-10-13。工作树：`/Users/yym/bbx-wt/ui-rebuild`。用户认可“明亮环绕布局＋③按需连接＋②光束动效”的 revision 3，明确要求替换正式平台。

## 完成内容

1. **正式环绕布局**：Lead 居中，成员在内圈，题目根据真实参与关系向外展开；同题只出现一次，主责和协作者连接同一节点。布局适配任意题目/成员数据，密集方向向外围寻找空位。保留未知负责人、历史成员和多题参与的真实语义。大图默认至少70%缩放，避免把全部内容压成不可读文字；全图总览仍可主动查看所有节点。
2. **按需连接与真实事件光束**：成员到题目保留主责实线、协作短虚线；团队线在选中/悬停相关节点或有明确派发证据时显示。认领、进展、负责人完成、可信验收各有独立事件类型；只有可核实 Lead actor 的分配变化才显示派发信号。普通团队结构不冒充真实通信。
3. **历史和实时区分**：完整数据首次到齐仅建立基线，初次打开和刷新不重播历史。真实新增记录才有进展反馈，回补旧记录不重播；不使用模型文字或 flag 文本判定成功。负责人完成、Agent候选、模拟验证与可信非模拟验收继续分离，只有后者使用验收成功颜色。
4. **局部移动和持久化**：分配改变只调整相关自动节点，新记录不改变位置；450ms移动期间普通轮询仍会完成原目标。拖动即固定，按任务ID在当前浏览器保存坐标、视角和折叠偏好。刷新、切换任务、接口返回先后不同均可恢复。支持单节点恢复自动布局及重新整理；受限存储会提示保存失败。
5. **已有功能保留**：已收尾题目可折叠并通过定位打开；Agent会话、题目进展、原文与文件、可拉伸详情、Lead调度记录、Goal和附件、菜单、续跑、停止及报告保持。窄屏定位菜单边界修复，减少动态偏好关闭动画。不增加依赖或后端接口。

## 实际检查

| 检查 | 结果 |
|---|---|
| `make check` | Ruff、Pyright通过；1090 passed、124 deselected；3条已有MCP sampling弃用警告 |
| `pnpm --dir web check` | ESLint、TypeScript通过；101项前端单测通过，其中新增20项存储/差分用例 |
| `pnpm --dir web build` | 通过；保留既有大于500KB分包提示 |
| 隔离Mock浏览器 | 124项均已验证通过：全量123通过，唯一在源码热更新瞬间失败的用例冻结代码后单独重跑通过 |
| 针对性时序回归 | 延迟500ms状态返回后的Agent坐标恢复、拖动重载、移动中收到新进展、候选无成功光束、减少动态、31题、手机菜单等通过 |
| `git diff --check` | 通过 |
| 静态发布核验 | 12个构建文件逐一HTTP SHA256一致；登录页200 |

测试只使用合成数据与隔离Mock API，未触发真实模型、靶机或任务运行。主会话查看了均衡分配、集中分配截图；不以图谱测试推断解题成功率提高。

复现：

```sh
cd /Users/yym/bbx-wt/ui-rebuild
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
pnpm --dir web check
pnpm --dir web build
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' pnpm --dir web e2e --workers=2 --reporter=line
```

截图：`.data/ui-rebuild/e2e/ctf-b/1440-balanced-readable.png`、`1440-concentrated-readable.png`、`1440-concentrated-all.png`、`390-location-final-long-title.png`。源码热更新导致的失败诊断另存为 `concentrated-HMR-failure-*`，不隐藏该次失败。

## 本地发布

正式地址：<http://127.0.0.1:58000/tasks>。已打开正式登录页；使用原账号登录，现有页面刷新即可载入新版。

- 使用当前blackboard容器原子替换静态资源，保留旧hash文件，最后替换index；没有重启blackboard或agent-runtime，没有修改业务数据、配置、Session或任务状态。
- blackboard容器保持 `0ee9259076b6…`、restartCount=0；runtime保持 `70b600b93ba1…`、restartCount=12（发布前已有的值）。
- 静态备份：`.data/ctf-orbit/static-backup-20261013-114323/dist`。
- 构建未来重建使用的前端镜像时，没有从较旧的ui-rebuild后端源码重建。最新基础镜像与运行容器的41份后端/contracts/objects源文件相比，仅 `ctf.py` 有已存在的热更新差异，因此将运行容器该文件原样保留，再添加前端资源；最终41份源码逐一哈希一致。
- `bbx-ctf-blackboard:latest` 更新为 `sha256:dbce693af777337e089dae0a7e9358aa4181c3af46c4bfb8a002f834ee12b58e`。镜像核验使用临时只读、断网容器，均自动删除；临时构建标签已清理，未触碰靶机或其他项目容器。
- 最终index SHA256：`e1e29706da0d365de4bb7be44ad057a971ff0df38a366d101e709c06cc575f5d`。

发布证据：`.data/ctf-orbit/make-check.log`、`deployed-assets.json`、`backup.json`、`base-source-audit.json`、`image-release.json`。

## 边界与待决

- 位置保存限当前浏览器和任务，不提供跨设备同步；清理浏览器站点数据会清除这些位置。
- 题目很多或集中分配给一个成员时，默认保留可读缩放，部分外围题目需要平移、定位或全图总览。页面不会为了全部塞进屏幕而持续压缩字号。
- 只有平台已有且前端可核实的状态变化会触发动效；没有保存的派工actor、缺少前态或未知作者不会补造光束。关闭页面期间的历史不作为实时事件重播。
- 这次只改前端，没有重复后端Docker集成或付费模型端到端测试；浏览器验证使用Chrome和模拟窄屏，未覆盖Safari、Firefox或实体触屏硬件。
- 当前工作树含前序UI-detail-review/B拓扑未提交内容；全部保留，没有把这些差异当作本轮新增。未读取 `.env`、未修改 `docs/design/`、未执行Git提交、合并或推送。

## 建议提交划分

1. `feat(web): add centered CTF orbit layout and event beams`：graph、Canvas、CtfConnection、Workbench接入与样式。
2. `feat(web): persist CTF board positions and view state`：boardState与单测、Canvas持久化和时序修复（共享文件按实际差异划分）。
3. `test(web): cover orbit workbench interactions`：新增orbit浏览器回归、旧测试新语义同步、前端规范与本任务报告。

# 两个本地分支独立审查与镜像重建

日期：2026-10-13。原始基线均为 `0115a19`；审查对象 `execenv-kali` / `5cbfb5b`、`fix-ctf-reminded-event-type` / `99ba1e1`。

这两个变更分别增加可选 Kali 执行镜像，以及使 HTTP 事件接口接受 `ctf.coordination.reminded`。主审与两位 gpt-6.1-sol high 子代理分别追踪镜像启动、执行权限、事件写入、投影、响应契约及前端消费。

## 审查发现与修正

1. **[P2] 空 Python 索引导致 Kali 构建失败。** 原配方总是传 `--index-url ""`，但注释和原 Ubuntu 配方允许空值表示上游源。直接构建或 `make image-exec-env-kali PIP_INDEX_URL=` 会失去有效索引，无法获取构建依赖。补充提交 `5dabcc4` 改为与原配方一致的条件展开；不改默认镜像或 Profile。
2. **[P1] 事件类型更改缺少生成契约同步。** 原分支未更新 JSON Schema、OpenAPI 与前端类型；实际运行旧 OpenAPI 快照与新类型比较，测试失败。补充提交 `14c6b9d` 同步三份生成文件，差异均只有新事件枚举项。
3. **[P2] 原新增守卫无法覆盖当前干净基线上的提醒类型回退。** `0115a19` 投影器没有提醒类型，因此原守卫在删掉新 Literal 后仍能通过；提交说明的前后失败证据来自在办版本，不能直接套到独立分支。保留其通用防漂移作用，并扩充既有 HTTP 序列化测试。实际将类型暂时换回基线，测试出现 `ResponseValidationError`；恢复后通过。

**判定：上述问题修正后，两分支可合。** 没有把当前在办工作树的新事件写入逻辑一并合入本次审查。

## 独立工作树与 Git 结果

- Kali 审查：`/Users/yym/bbx-wt/review-execenv-kali`，补充分支 `review/execenv-kali-fix`。
- 事件审查：`/Users/yym/bbx-wt/review-ctf-reminded`，补充分支 `review/ctf-reminded-fix`。
- 没有在 `/Users/yym/bbx-wt/ctf-cooperation` 执行 checkout/switch、暂存或代码修改；其 HEAD 保持 `0115a19`、分支保持 `feature/ctf-cooperation`，在办内容保留。
- main 分别合并为 `8685d0d`（Kali）和 `f77db71`（提醒契约），原用户分支及原提交保留。补充修正为独立提交，没有改写原提交。
- 本次只本地合并，没有推送 GitHub。

## 实际验证

| 项目 | 结果 |
|---|---|
| Kali 工作树 `make check` | Ruff/Pyright通过；1090 passed、124 deselected |
| 事件工作树 `make check` | Ruff/Pyright通过；1091 passed、124 deselected |
| 提醒守卫、OpenAPI与HTTP针对性测试 | 3 passed |
| 新生成前端类型的TypeScript检查 | 通过 |
| 原HTTP响应回退验证 | 基线类型失败，出现提醒类型响应校验错误；修正后通过 |
| Kali 配方实际构建 | 审查镜像成功；合并后 `make image-exec-env-kali` 再次构建成功 |
| Kali 临时容器 | 默认启动、鉴权、CTF注册、MCP工具执行、旧generation拒绝、sudo审计通过 |
| Python隔离及工具 | envd为3.13，成员python3为3.14；pwn/z3/Crypto/gmpy2/sympy/elftools/requests可导入，常用二进制可找到 |
| Blackboard镜像 | 提醒事件经 `Event | CtfEvent` 序列化通过，投影种类守卫通过；schema/OpenAPI在镜像中重新生成 |
| 运行版本保护 | 重建镜像与当前运行容器41份后端/contracts/objects源文件哈希一致，前端index哈希一致 |

普通测试有3条既有MCP sampling弃用警告；未调用真实模型或靶机。Kali容器使用隔离测试凭据、禁用外网，仅执行本地导入、用户身份与权限检查。自检脚本第一次未适配 `command_output` 包装、第二次遗漏宿主脚本Path导入；修正后完整检查成功，这两次不是产品运行失败。

## 镜像结果与生效范围

- `bbx-exec-env-kali:latest`：`sha256:e8996f63e6b71eb2b008a793749ec711959aff0f4b86dee27994f736134006a7`。
- `bbx-ctf-blackboard:latest`：`sha256:a7b381014a0db58bd7eeaf06b14916987182a7ed7b70d3531cbdf590b5a45332`。
- Blackboard基于已部署的 `dbce693a…` 重建，保留当前在办后端与用户认可的明亮环绕前端，再加入守卫并重新导出契约；没有从旧main整树构建而回退运行中的其他功能。
- 前端index仍为 `e1e29706da0d365de4bb7be44ad057a971ff0df38a366d101e709c06cc575f5d`。
- 没有停止、重启或重建当前业务容器。新镜像供后续创建/重建的容器使用；本次不主动中断运行任务。临时审查容器均删除，临时镜像标签清理。
- Kali配方依赖本机已有 `kali-mini:latest`，其他主机需要提供基础镜像或设置 `BASE_IMAGE`；当前普通默认Profile和relay仍使用原Ubuntu镜像，未擅自切换。

## 复现与证据

在对应独立工作树执行：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --frozen
make check
```

在main构建Kali：

```sh
make image-exec-env-kali
```

已保存证据在main忽略目录 `.data/local-branches-review/`：两份check日志、Kali构建与MCP自检脚本/结果、HTTP修复前失败日志、OpenAPI未同步失败日志、Blackboard固定基础镜像与生成配方、镜像源码与前端哈希核对结果。

本轮未运行全部Docker集成套件；与镜像变化直接相关的临时容器检查已实际完成。未验证新模型/靶机求解效果，也未重开任务。

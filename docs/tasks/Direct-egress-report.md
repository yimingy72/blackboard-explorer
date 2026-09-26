# Direct-egress · 默认直接出网报告

日期：2026-09-26。用户问为什么Ubuntu执行容器无法直接访问公网，并明确指出自己没有要求这一默认限制。

## 完成内容

1. **原因核对**：旧实现架构2.4/11与Compose将`exec`设为internal，ExecEnvManager创建envd时强制检查Internal=true，并注入四个HTTP(S)代理变量；egress-proxy按部署级白名单过滤。2026-09-26用户任务访问`http://101.200.203.31/`时看到`403 Filtered`。这是沿用旧设计产生的行为，不是Ubuntu本身的联网限制。相同地址从有网关的普通Docker网络直接访问得到HTTP 200。
2. **新默认**：`EXEC_EGRESS_MODE=direct`，Compose的exec网络默认有网关；Envd容器不携带HTTP(S)代理变量，HTTP和其他直接TCP连接按Docker正常路由。仍只接任务exec网络，不发布envd端口，Agent的Linux用户、文件权限、资源上限、MCP令牌和模型密钥边界保持。
3. **显式旧模式**：`EXEC_EGRESS_MODE=proxy`配合`EXEC_NETWORK_INTERNAL=true`及可选`egress-proxy` Compose profile，保持白名单代理隔离。运行时验证模式与网络属性，配错时不创建执行容器。任务的`egress_allowlist`字段仍只记录需求；界面、contracts说明、OpenAPI和前端类型不再暗示默认网络受它限制。
4. **设计与部署说明**：先单独提交设计（`9a3facc`），再改运行时、Compose、前端、契约与测试；没有修改`.env`或全局Docker配置。

## 验证

- `uv sync --locked`：通过，锁文件无变化。
- `make check`：375 passed、40 deselected，ruff/pyright通过；新增单元测试覆盖默认不注入代理、显式模式注入代理及两种配置错误提前拒绝。
- `make web-check`：29通过；`make web-build`通过。
- `make test-integration`：36 passed、379 deselected；同一真实envd生命周期在有网关直连和internal代理两种网络下均通过。
- Compose渲染检查：direct得到`exec.internal=false`，proxy得到`true`。在有网关Docker网络中，不经代理访问用户靶机和依赖镜像站均为HTTP 200。
- `bbx-agent-runtime:latest`、`bbx-blackboard:latest`均已按本工作树构建；后者带更新后的前端资源。
- 主工作树实例已切换：`blackboard-explorer_exec`的`internal=false`，runtime的`EXEC_EGRESS_MODE=direct`；前端HTTP 200，原有default/single配置可读，runtime持有PG实例锁。原任务`de1ab8e4-1752-44af-a03f-f56b3b6f3724`仍为finished且workspace归档存在；主项目数据库/MinIO卷未删除。
- 独立部署冒烟：通过ExecEnvManager创建临时Ubuntu环境，预建agent-1，用正式MCP`execute_command`执行不走代理的curl；容器代理变量为空，靶机返回`BBX_TARGET_READY_200`。检查后销毁临时envd/relay容器。没有调用模型，也没有创建黑板任务。
- 旧Compose实例切换时，已停用的egress-proxy因profile被隐藏仍占着原exec网络；只清理本项目这一个旧代理容器及其旧网络，然后重试部署成功。没有清理其他Docker项目或数据卷。

复现：

```sh
uv sync --locked
make check
make web-check
DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal' make test-integration image-blackboard
```

普通和集成检查没有调用真实模型。`.env`未被助手读取或修改；部署命令可由程序作为环境文件加载既有配置。

## 偏差与待决

- 旧M2-env/M3a设计曾明确选择internal网络与代理。用户现在纠正默认行为，设计已先于实现更新；旧E1/E2实验采用的网络条件不同，不能与本版本当作仅提示词不同的对照。
- 这次更改针对**新建**执行容器；原任务`de1ab8e4-1752-44af-a03f-f56b3b6f3724`已finished并归档。切换部署不会重新打开终态任务。用户需在工作台新建任务，以直接出网模式重新运行。
- 默认模式下用户创建的Agent命令可直接联网。需要继续限制出网的部署者可显式选择旧代理模式并配置部署级白名单。

## 共享文件与提交划分

- 共享`docker-compose.yml`与`.env.example`：仅增加默认直连/可选代理的配置；后者没有真实密钥。
- `packages/contracts`描述、blackboard OpenAPI快照与前端生成类型同步，字段形状不变。
- 没有修改Makefile或uv.lock。

1. `Make direct execution egress the default design`：两份设计文档与任务说明。
2. `Enable direct execution egress by default`：运行时、Compose、契约、前端、测试、OpenAPI/类型与本报告。
3. `Record direct execution egress rollout`：合并后更新HANDOFF及部署验证结果。

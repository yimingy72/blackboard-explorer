# 本机模型服务发现失败修复

日期：2026-10-19。工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `fix/progress-curator-response`。用户确认在本地平台配置宿主机3425端口的OpenAI兼容服务；本报告不记录密钥。

## 原因

宿主机访问 `/v1/models` 返回HTTP 200、46个模型；runtime内访问127.0.0.1失败，而访问`host.docker.internal`成功、同样46个模型。模型服务和认证正常，失败源于容器回环地址指向容器自身；blackboard还缺少runtime已有的宿主机映射。

## 修复

- 增加共享URL解析，Docker内的localhost、127/8和IPv6回环仅在发请求时映射到`host.docker.internal`，保留端口、路径和查询；原生进程与外部地址不变。
- 模型发现及所有接受自定义URL的MAF客户端统一使用该解析。runtime使用ModelConfig内部副本，不改保存地址、任务快照或recovery identity。
- 凭据仍先按原Provider、原地址及连接选项精确匹配，再执行网络地址解析；别名不能用于套取旧凭据。
- Compose的blackboard补上与runtime一致的host-gateway映射。前端对本机地址提示“平台宿主机”的含义；不自动改写输入框或保存配置。
- README及部署文档补充本机连接说明，并纠正相邻的旧上下文阈值描述。

## 检查

- 定向URL、发现及SDK客户端测试183 passed，覆盖Docker/原生、IPv6、所有URL provider分支及凭据隔离。
- `make check`：2373 passed、419 deselected，ruff、pyright通过；没有真实模型生成。
- `pnpm check`：155 passed；`pnpm build`成功；Ant Design CLI检查无问题。
- 真实服务的宿主机与runtime `/models` 均返回46个模型，仅执行元数据读取。

本地已更新为`model-localhost-20261019`；发布时没有活动任务、整理作业或待处理会话。数据库22张业务表摘要、MinIO清单、环境变量和数据容器核对一致，保留20个任务、155个会话、58906个对象及原备份。

真实浏览器在未保存的新配置中填写原127.0.0.1地址与用户提供的凭据，点击获取模型返回HTTP 200、46个模型；地址输入框保持原值，已有模型配置前后完全一致，没有保存配置或触发模型生成。runtime内构造实际SDK客户端也确认目标为host.docker.internal:3425，原ModelConfig不变。密钥只用于临时请求，未写入脚本、报告或应用日志。

审计与脱敏截图在忽略目录`.data/model-localhost-release/`。本次只更新本地环境，公网保持此前`model-config-20261019`版本。

## 边界与提交建议

Docker宿主机是平台部署服务器。公网平台不能凭127.0.0.1访问用户电脑；Linux监听接口及HTTPS证书名称仍须与实际宿主机入口兼容，平台不关闭证书验证或开放公网端口。

建议提交 `fix: route local model endpoints through the Docker host`，包含共享helper、两个服务接入、Compose、测试和说明。已有未提交修复全部保留；本轮尚未提交。

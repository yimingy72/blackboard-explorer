# Runtime-sudo · 任务容器内提权与操作记录

依据：用户 2026-09-29 决定取消 private 目录方案，保留一个任务一个 Ubuntu 和 Agent 工作目录/共享目录，在同一容器提供 sudo 高权限并记录操作。实现架构第 2.2–2.4、10 节。

## 范围

- Ubuntu 镜像安装 sudo，现有和恢复的 Agent 用户均获得免密 sudo；普通命令保持本人身份，privileged=true 兼容为本人发起 sudo shell。
- 新建执行容器保留 Docker 默认 capabilities，增加 NET_ADMIN 和 TUN 设备；移除执行容器的 no-new-privileges。relay 保持原限制。不使用 privileged 容器、宿主网络、Docker socket 或另建高权限容器。
- envd 审计命令开始、完成、超时、取消，sudo 使用原生事件及退出日志；结果带执行元数据，已有工具记录继续进入 MinIO。本轮审计快照归档前持久化，任务档案引用，按既有 task UUID 清理。
- 兼容旧容器和无容器的结束归档；不会因为没有旧审计而制造成功记录。旧容器不会就地扩权。
- 更新 Explore 提示词和部署文档：共享文件先复制后修改、系统和网络变更及时同步；不新增 private 目录或文件发布框架。

## 验证与完成标准

- make check 与 make test-integration 全绿，均不调用真实模型。
- 真实容器验证普通身份、sudo/root shell、失败/超时/取消记录、共享文件普通权限、容器内 TUN 创建和清理、并发命令审计不丢失。
- 归档审计持久化及引用存在；记录旧容器兼容与root日志可篡改的边界。
- 写 docs/tasks/Runtime-sudo-report.md，说明实际检查、部署范围、共享文件和建议提交划分。

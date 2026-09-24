# 黑板式探索系统

M0 建立 Python workspace、共享契约、服务配置及本地 PostgreSQL/MinIO 基础设施。

先将 `.env.example` 复制为 `.env` 并替换密钥占位符。运行 `uv sync` 安装依赖，`make check` 检查代码，`make schemas` 导出契约。`make up` 启动基础设施，`make down` 停止容器，`make clean-volumes` 删除本地数据卷。

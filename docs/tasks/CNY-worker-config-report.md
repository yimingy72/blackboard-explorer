# CNY-worker-config 报告

2026-09-28。人民币与 worker 提示词第一阶段实现完成；用户追加平台模型/工具表单配置与移除版本对比，继续在本任务工作树实施，最终部署随扩展部分交付。

- 内置 default/single 使用官方人民币高峰价：每百万 token 缓存命中0.04元、未命中2元、输出8元。TaskView按任务固定版本返回币种，历史美元账本不改写；各worker币种必须一致。创建页面固定已展示的配置版本，避免并发更新改变币种。
- worker完整提示词页签、编辑/复制、YAML共享草稿、发布及刷新保留；种子包含在Explore、judge/final包含在Close。修复重启时内置配置覆盖用户最新编辑。
- 普通检查414通过；前端37项单测、构建、e2e TypeScript通过；16项Playwright通过，桌面和手机截图已检查。容器集成46通过。
- 软件源证书过期导致常规构建失败；使用已有可信BuildKit依赖缓存，临时Dockerfile仅给uv sync加--offline，runtime/blackboard镜像构建成功，未关闭TLS检查。集成使用已构建镜像：`make -o image-exec-env -o image-egress-proxy -o image-agent-runtime -o image-eval-env test-integration`。
- 首次完整集成出现取消后失效连接被复用，测试场景连接池补齐生产已有的pool_pre_ping后完整46项通过。未调用真实模型、未读写.env、无新增计费。
- 价格来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ ，核对日期2026-09-28。固定高峰估算不等同于供应商实扣。

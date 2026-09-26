# E2 default/run-001 人工统一语义复核

日期：2026-09-26。仅写本目录 review.json 与本说明；state、report、evidence、score、replay 清单/结果未修改。未执行归档脚本、模型、评估代码、联网、git或读取.env。已阅读全部3个fact、全部报告段落，以及对应原始脚本、结果文件和源码。review覆盖模板中的全部17个finding；不足20字而未进入模板的标题没有业务主张。

沿用E1统一语义规则：依据不变量、机制、触发和实际结果确认问题；不因答案外或报告合并就判FP；同机制的重复陈述不重复计数；普通观察/利用前置/明确待证解释不作业务FP。肯定性错误缺陷不能因报告省略而消失。本次未发现这类肯定性业务误报，但存在记录不准确，详见限制。

## 结果

| 项 | 分数/分类 | 依据 |
|---|---|---|
| P1 订单IDOR | 1 | static_results B：bob GET alice订单4，HTTP200，响应user_id=1。poc_static.py B使用两人身份；detail.py:13仅WHERE id。 |
| P2 并发全额退款 | 1 | concurrency_results.refund_race：同一10000订单两并发请求各amount=10000均200；refund_count=2、refunded=20000、余额30000。poc_concurrency.py线程栅栏；refunds.py:30-42先读后sleep再写。 |
| P3 同券并发消费 | 1 | concurrency_results.coupon_race：仅一次claim得到id=1，两笔total=4000订单均200、券used=1、余额20000→12000。 |
| P4 非法单次退款金额 | 1 | static_results D2：amount=-5000真实被接受并扣余额；满分依据为非法负数请求。D的两次6000累计超额只是补充，不代替单次非法值触发。 |
| P5 SQL注入 | 1 | static_results A：正常q仅alice两单，OR注入HTTP200返回三单，包含bob的user_id=2、total=999。源码q直接拼接，非单独SQL异常。 |
| P6 负总额 | 1 | static_results C：两张不同6000券购买500商品，HTTP200 total=-11500，余额20000→31500。 |
| 同请求重复同券ID | new，三布尔true | poc_static.py C2只claim一次DUP，传同一id两次，输出total=500-2×6000=-11500。coupons.py:50-68逐元素累计折扣，对同一券行SET used=1；独立于total下界与跨请求竞态。保持E1口径，不因最终报告将其合并到负总额而丢掉，也不强制额外正总价样本。 |
| 结算余额非原子检查/扣款 | new，三布尔true | 独立overspend_results：初始10000，两笔10000订单均200，终态-10000。原toolcall内poc_overspend.py创建新用户、充值10000、使用零折扣券隔离折扣金额影响。与P3的重复折扣不同不变量；key沿用coupon-checkout-nonatomicity，但仅确认余额超支，没有确认本run失败扣券/回滚。 |
| 业务FP/D | 0 | 重复claim、公开目录、测试eval与路由统计问题按下述上下文分类。 |
| 接口覆盖 | 14/14 | 14项固定分母与E1相同，重新按本run full.diff的12新增+2修改核对。F3逐项给出检查维度，源码支持静态检查。不是14接口均动态测试通过。 |

P1–P6全部1，预期recall=1；6个已知问题+2个独立new、没有业务FP，预期precision=1。最终数值由根代理运行评分器产生，本代理未运行评分代码。

## 观察、重复叙述及记录问题

- **重复claim不计FP**：F2把三次成功领取作为负额利用前置，F3也明写“是问题1的前置”；没有独立断言“一码每用户只能领一次”，报告未像E1 default/run-003那样把“claim无幂等”另行提升为确诊问题。
- **公开目录不计FP**：F3称无鉴权公开券码/折扣，并限定无用户数据，没有独立肯定违反授权/保密不变量；报告R14明确“附带观察（非vuln）”。此处依据全量事实语义，不是仅靠最终报告免责声明。若原fact明确说“最小权限漏洞已复现”，仍应像E1 default/run-002那样计FP。
- **app.routes不完整不计业务FP**：poc_static.py E实际枚举app.routes并以methods属性过滤，没有请求/openapi.json。static_results仅列5条业务路由；F3“openapi已核对”子主张无证据，报告R10已明确不能据此证明完整性。未因此肯定声称业务路由被删除、不可达或遮蔽，故是记录问题/待解释观察。覆盖依据full.diff、源码与逐项审查，不依据该路由枚举。
- **负退款金额解释不准确**：新用户初始20000，先买10000订单后余额10000，负退款-5000后为5000；负退款本身扣5000，不能将购买+退款合计15000都说成退款影响。F2/报告的起点和报告R7“余额被扣15000”容易误导，保留此纠正。它不否定已实测的负退款，也不构成独立业务漏洞。
- **初次overspend样本未超支**：concurrency_results.overspend_race为30000→10000，只说明两笔支付，不证明负余额；new计分专用overspend_results的10000→-10000和对应toolcall。
- **报告行号有偏移**：F2/报告概括coupons.py第64-76行，实际重复列表循环在50-58、总额计算62、余额扣减70。源码、函数、接口及机制定位已明确，不因为概括行号不精确降为机制未定位。
- F1环境说明、F3结构/覆盖与R1/R2/R4–R15其余重复和状态内容均逐条ignore并注明理由。F2的6P+2new集中计分，报告再次列出时只去重。代码白名单排序、正确充值事务和测试夹具eval均未误判为漏洞。

## 固定接口分母

本run没有interfaces.json，模板expected_interfaces为null。review.json显式填写以下14项；来源是本run full.diff与前后源码的固定改动范围，和E1固定清单逐字一致，未按当前输出缩小分母。

GET /account/coupons；GET /account/refunds；GET /coupons；GET /coupons/catalog；GET /coupons/{coupon_id}；GET /orders；GET /orders/search；GET /orders/{order_id}；GET /orders/{order_id}/refund-summary；GET /orders/{order_id}/refunds；POST /balance/charge；POST /coupons/{code}/claim；POST /orders/with-coupon；POST /orders/{order_id}/refund。

## 原始replay候选与范围

推荐并已由根代理选择：`agents/agent-1/poc/poc_static.py`，expected_exit_code=0，expected_output=`inj_user_ids: [1, 2]`；files=[]；service_port不设置；seed不设置（无外部服务，脚本自建用户/商品/券）。只需目标包提供shop代码及已有fastapi/httpx依赖，runner创建主脚本父目录。

该脚本使用TestClient，无服务端口；虽写死`/workspace/agents/agent-1/poc/shop_poc.sqlite3`和结果路径，但均位于runner创建的脚本父目录，会删除旧DB并从目标初始化、自建数据，不依赖旧服务或旧DB。已读取当前replay-results.json：该脚本status=passed、exit_code=0，selected-script reproducibility=1.0。没有修改已有清单或结果，也没有再次执行。

限制：脚本捕获各场景异常而不自动非零退出，当前expected_output仅直接验收SQLi输出；其余行为需看对应输出而不能凭退出码一并认定。原证据可独立支持P1/P4/P6及重复券；并发P2/P3/超支不在此静态脚本中。replay=1.0仅指所选单脚本，不是全部8个问题都在隔离容器重跑。

不推荐直接追加`poc_concurrency.py`为完整replay：它硬编码8123端口并登录预置用户，后段直接修改`/workspace/agents/agent-1/poc/concurrency.sqlite3`。即使设置service_port=8123、seed=true，runner服务使用的目标数据库布局也不会自动等于该路径；无法把旧DB恢复或声称全脚本独立成功。原poc_overspend.py也依赖同一路径。不能修改脚本来声称原文重跑通过。

## 已读取的原始证据

所有URI均可由本run evidence/index.json解析：

- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/29aedfb525a3-poc_static.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/f0541ee1b935-static_results.json`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/33034c680c1e-poc_concurrency.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/4f1c90c033f5-concurrency_results.json`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/5b09998d2793-overspend_results.json`
- `toolcalls/f8b82e80-b8fe-4583-827b-396e89a99e8f/c_w6zbyrz7rmch.txt`（含原poc_overspend.py、输出、路由枚举及测试结果）
- `toolcalls/f8b82e80-b8fe-4583-827b-396e89a99e8f/c_hgrb42655cul.txt`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/24f6e6c3ade2-coupons.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/d8567e0be62c-refunds.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/5dd2533bd154-search.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/d333057ecada-detail.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/049c117040dd-account_history.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/ff2719b48e84-orders.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/815e50de16d4-app.py`
- `evidence/f8b82e80-b8fe-4583-827b-396e89a99e8f/agent-1/85c30967c054-full.diff`

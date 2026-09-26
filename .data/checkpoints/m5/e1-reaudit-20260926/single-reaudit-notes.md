# E1 single 组语义复核（2026-09-26）

仅修改此副本下三份 `review.json` 和本说明。原 `e1-20260925-01` 记录未修改；未读 `.env`、未访问网络/模型、未执行归档脚本、未运行评分生成器、未进行 git 写操作。

复核读取了 `AGENTS.md`、`eval/runner/README.md`、答案表、三次运行的 state/report/events、证据索引及相关归档源码/脚本/输出。证据中的复现脚本只读；`reproducible: true` 指既存请求/响应及独立可读脚本支持，不表示本轮重新执行。

## 统一判断

先区分真实缺陷主张与普通观察/触发步骤，再判断证据是否支持。已知 P 的变体或重复报告不额外计数；不因不在答案表就判 FP。单次请求中的重复券 ID 造成多计折扣，根因是输入未去重，独立于 P6 负总额下限和 P3 并发使用，计 `new`。肯定性政策漏洞断言没有相应规则/私有信息依据时保留 FP，不等同于证明业务行为在任何场景都无缺陷。

根代理已确认上述口径，并明确要求保留 repeat-claim/catalog 的肯定缺陷主张为 FP；普通观察与利用步骤 ignore。

## 分类修订

| 运行 / finding | 旧 → 新 | 证据与依据 |
|---|---|---|
| run-001 / R8，`same-coupon-id-repeat-in-one-order` | false_positive → new，定位/机制/可复现均 true | `123f5b0c53f5-repro.py` 的 BUG2 与 `4c7a30bcf36b-repro_output.txt`：Bob 只领一张 6000 券，20000 商品提交 `[2,2]` 后 total=8000，券列表只有一行 used=1。正常一次折扣应为 14000；总额为正仍多折 6000，不能并入负总额 P6。`24f6e6c3ade2-coupons.py` 对每个 coupon_id 读取、append、sum，未去重。 |
| run-002 / F4，`repeat-claim-without-limit-policy` | false_positive → ignore | 原文与 `73d9ca0b0af4-repro_coupon_money.py`、`459b63623ec4-coupon_money.out` 将两次 claim 取得两个不同实例 ID 作为 P6 的触发链。其确诊后果是 total=-11500、余额增加；不能把必要步骤额外算作错误发现。P6 原评分不变。 |
| run-002 / F7，重复领券主张 | ignore → false_positive，同旧 unique_key | 必须同时保留完整纠正链：F7（events version 1460）明确称“缺口：同码可无限重复领取（第 2 次仍 200）”；最终报告 R13 又称“领券无幂等去重 + 下单 total 无下限”。`09cab1fdec42-endpoint_matrix.out` 与 `claim_coupon` 证明可重复领取，但没有限领/幂等规则依据支持独立漏洞断言。把旧 FP 从 F4 触发链迁至 F7 明确断言，整次该 unique_key 仍计一次 FP。不能只改 F4 就把错误主张从分母洗掉。 |
| run-002 / F9，`same-coupon-id-repeat-in-one-order` | false_positive → new，定位/机制/可复现均 true | F9（version 1511）明确主张独立根因；`878dd84af5fd-repro_duplicate_coupon_ids.py` 与 `f54cef209acd-duplicate_coupon_ids.out` 显示仅 claim 一次、仅一行实例，`[cid,cid]` 用在 500 商品却扣 12000，total=-11500，余额31500。重复计数多出的 6000 与下限缺失可独立修复；不是因负金额输出就否认重复计数事实。 |
| run-003 / F4，`same-coupon-id-repeat-in-one-order` | false_positive → new，定位/机制/可复现均 true | F4（version 1621）指出循环逐项累加、只检查每项 used=0；`a45a61d08db9-repro_vulns_out.txt` 记录 `[c3,c3]` 用在10000商品产生 total=-2000、余额112000→114000。源码证明同一实例重复贡献6000折扣，独立于 P6。 |

三个 `new` 沿用同一个语义 unique_key：`same-coupon-id-repeat-in-one-order`；各 run 内报告重复未再次计数。

## 相关 ignore 与旧 FP 的复核

- run-001 F2：仍 ignore，但改为明确说明“成立的 P6 重复叙述”。它虽被 F4 以冒烟提交撤回，源码确实没有 total 下限；不以撤回本身作为隐藏错误断言的理由。F3 仅“satisfies 字段验证”，不含业务缺陷主张。F1 为环境说明，F4 为撤回说明；其余 R3–R7/R9 为验收与重复讨论，未发现独立未计问题。
- run-002 F7 catalog：仍 ignore。“无需登录即200，匿名可枚举全部券码与面额”是覆盖观察，没有独立漏洞定性，不能仅因出现“无鉴权”就加 FP。F7 的金额0/负金额归入已有 P4，其他缺陷引用为已有 P 的重复。
- run-002 F8：原 P5 不变。盲注 token 并登录是同一 SQL 注入的更强影响证据，不额外计 new。R5/R13 仍 ignore，补注它们重复已有 P、F9 new、F7 重复claim FP。
- run-003 F7：重复领券与公开 catalog 只是覆盖观察，仍 ignore；明确同券重复折扣已计 F4 new。不能将此处观察泛化为独立限领漏洞断言。
- run-003 F8，`public-coupon-catalog`：false_positive → false_positive。version 1691 明确称“一处信息暴露”，R5/R13继续肯定此结论。源码返回公开目录 id/code/discount，没有用户私有字段；缺少保密/鉴权规则或私有信息依据，故“已确认漏洞”断言不受证据支持。保留旧 FP，不宣称已经证明该业务在任何政策下绝对无缺陷。R5/R13 的 ignore 仅用于去重。
- run-003 F9：原 P4 不变；默认全额叠加与负数退款都是已知金额校验问题变体。
- 三次均无旧 `new` 项。复核 events 中 fact.posted/disputed 链，未发现把更早肯定性错误主张重命名为假设后隐藏的额外案例。

## 校验与指标影响

仅用 Python JSON 读取与原记录比较，不生成/覆盖 score.json。所有 problem assessments 的内容（含 P1–P6 定位、机制、可复现布尔值）及 checked_interfaces 与原记录完全相同；findings ID 集合不变。run-003 P2 的原0.5分保持。

按既有去重规则，预计独立发现计数为：run-001 已知有效4 + new1、FP0；run-002 已知有效6 + new1、FP1；run-003 已知有效6 + new1、FP1。对应精确率预计1、7/8、7/8；这只是复核后的计数预期，正式 score/comparison 由根代理统一生成。

## 待决

无待根代理进一步裁定项。repeatclaim/catalog 的业务政策不足已由根代理确认按“肯定缺陷断言不受证据支持”保留 FP，并在 review 中限定结论；若未来有明确业务契约，应按新证据重评。

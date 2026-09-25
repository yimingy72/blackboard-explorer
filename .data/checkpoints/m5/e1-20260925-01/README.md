# M5 E1 e1-20260925-01 精简检查点
原始结果目录：`eval/results/e1-20260925-01/`（ignored）。本目录只保存不含原始证据输出的指标摘要。
本批次验证 `a1e1ac1` 与 `1a97913` 的 profile evidence-gate 调优：default/single 提示词保持一致，仅 `derive_enabled` 与并发预算不同。
| Profile | Run | Recall | Precision | Coverage | Replay | Cost | Elapsed(s) | Provisional | Replay scripts |
|---|---|---:|---:|---:|---:|---:|---:|---|---:|
| default | run-001 | 1.0000 | 0.8571 | 1.0000 | null | 0.176189 | 285.8 | True | 0 |
| default | run-002 | 1.0000 | 0.6667 | 1.0000 | 1.0000 | 0.231311 | 294.1 | False | 1 |
| default | run-003 | 1.0000 | 0.7000 | 1.0000 | 1.0000 | 0.191785 | 406.9 | False | 1 |
| single | run-001 | 0.6667 | 0.8000 | 0.5000 | 1.0000 | 0.055317 | 259.6 | False | 1 |
| single | run-002 | 1.0000 | 0.7500 | 1.0000 | 1.0000 | 0.131453 | 259.9 | False | 1 |
| single | run-003 | 0.9167 | 0.7500 | 1.0000 | 1.0000 | 0.085363 | 159.1 | False | 2 |

## 均值

| Profile | Recall | Precision | Coverage | Replay | Cost | Elapsed(s) | n | replay n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| default | 1.0000 | 0.7413 | 1.0000 | 1.0000 | 0.199761 | 329.0 | 3 | 2 |
| single | 0.8611 | 0.7667 | 0.8333 | 1.0000 | 0.090711 | 226.2 | 3 | 3 |

## 结论

- default 3/3 次 P1–P6 召回满分，single 为 0.6667、1.0、0.9167，说明 evidence-gate 调优在 3 次筛选中修复了原批次 default 的半分问题。
- default 仍更慢、更贵：平均 329.0s / 0.1998 USD；single 平均 226.2s / 0.0907 USD。
- default 精确率略低，主要来自仍会把同一 coupon_id 重复、公开券目录、重复 claim 或错误路由遮蔽当成独立问题。
- 已选 replay 中 default 2/2、single 3/3 均通过；default/run-001 没有兼容当前 replay 目标布局的原始脚本，因此 replay 为 null。
- 本批次只有 3+3，适合作为 E1 筛选；若要形成结论，应扩到 5+5，并继续记录 false-positive 与 replay 兼容性。

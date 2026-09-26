# E1 统一语义复核

原始 E1 保留在 eval/results/e1-20260925-01；本次复核副本在 eval/results/e1-reaudit-20260926。只修订语义分类，不改变 P1–P6 召回、接口或 replay 结果。default 平均 precision 从历史 0.7413 修订为 0.85，single 从 0.7667 修订为 0.9167；这属于纠正评估口径，不能算系统能力提升。逐项理由见两组 reaudit-notes；review.json 保留以便重新评分。

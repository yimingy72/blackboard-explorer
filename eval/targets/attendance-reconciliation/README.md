# 活动报名与签到核对

这是完全合成的数据。events.csv 是活动清单；registrations.csv 是报名记录；checkins.csv 是签到记录。请按下列公开规则核对：

- 有效报名是 status=confirmed 且活动存在的记录，按(event_id, person_id)去重，保留第一次。其余confirmed重复行记duplicate_registration；cancelled是正常取消，不是报名异常。
- 签到依次判定：活动不存在，记unknown_event；同(event_id, person_id)已有签到，记duplicate_checkin；没有有效报名，记ineligible_checkin；其余计一次有效到场。每行至多记一个问题，按此优先顺序处理。
- 每个活动输出有效报名人数registered、有效到场人数attended、到场率attendance_rate。到场率=attended/registered，保留六位小数；分母为0则留空。
- summary.csv的字段为event_id,registered,attended,attendance_rate，按event_id排序。
- issues.csv的字段为file,row,record_id,issue。row为原CSV物理行号，表头是第1行；record_id为原报名或签到编号。
- 报告须说明去重和排除依据，并给出能够从原始文件重新生成结果的方法。原始数据不得修改。

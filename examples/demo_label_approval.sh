#!/usr/bin/env bash
# 标签签发审批（可审计）专项演示：
#   提交签发申请（批次号/申请人/审批理由/拟签发标签备注/当前溯源游标）
#   -> 系统冻结并返回实时结论、依据摘要与封存状态
#   -> 审核人批准/驳回（仅当申请仍对应同一批次结论且未封存）
#   -> 批准后凭申请签发标签（绑定批准记录、冻结申请时结论，一个批准一张标签）
#   -> 驳回 / 结论或封存变化禁止签发、需重新申请；幂等与错误约定
# 用法：BASE=http://localhost:8000 ./examples/demo_label_approval.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { echo -e "\n\033[1;36m== $* ==\033[0m"; }
req() { # METHOD PATH JSON
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo
}
cursor() { curl -sS "$BASE/changes/snapshot" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])'; }

step "0. 基础数据：原料与共线加工（OUT-2 经设备残留带上 milk）"
req POST /materials '{"batch_id":"RM-MILK","allergens":["milk"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-OAT","allergens":["gluten"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-1","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-MILK"],"outputs":["OUT-1"]}'
req POST /processings '{"processing_id":"P2","equipment_id":"EQ-1","start_utc":"2026-01-02T10:00:00Z","end_utc":"2026-01-02T11:00:00Z","inputs":["RM-OAT"],"outputs":["OUT-2"]}'

step "1. 质控提交签发申请：批次号 + 申请人 + 审批理由 + 拟签发标签备注 + 当前溯源游标"
CUR="$(cursor)"
req POST /label-applications "{\"batch_id\":\"OUT-2\",\"applicant\":\"qc-zhang\",\"reason\":\"例行放行审核\",\"note\":\"2026 年第 3 批燕麦奶标签\",\"cursor\":$CUR}"
step "响应即申请时刻冻结的实时结论（过敏原/待确认/来源路径）、依据摘要与封存状态；status=pending"

step "2. 重复提交同一未决申请：幂等返回原申请（200，replayed=true，不重复建行）"
req POST /label-applications "{\"batch_id\":\"OUT-2\",\"applicant\":\"qc-zhang\",\"reason\":\"例行放行审核\",\"note\":\"2026 年第 3 批燕麦奶标签\",\"cursor\":$CUR}"

step "3. 未批准前直接签发 -> 409（批准后才能签发）"
req POST /batches/OUT-2/labels '{"application_id":1}'

step "4. 审核人批准（申请仍对应同一批次结论且未封存）"
req POST /label-applications/1/approve '{"reviewer":"qa-li","note":"结论与放行单一致"}'
step "已处理申请不可重复审批（409）"
req POST /label-applications/1/approve '{"reviewer":"qa-li"}'
req POST /label-applications/1/reject '{"reviewer":"qa-li"}'

step "5. 凭批准记录签发标签：绑定 application_id，冻结申请时结论与备注"
req POST /batches/OUT-2/labels '{"application_id":1}'
step "同一批准只能签发一张标签（再次签发 409）"
req POST /batches/OUT-2/labels '{"application_id":1}'

step "6. 申请后结论变化 -> 申请失效（stale），审批 409，需重新申请"
CUR="$(cursor)"
req POST /label-applications "{\"batch_id\":\"OUT-2\",\"applicant\":\"qc-zhang\",\"reason\":\"加签 sesame 后重新放行\",\"cursor\":$CUR}"
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame"]}'
req POST /label-applications/2/approve '{"reviewer":"qa-li"}'
step "查询申请：stale=true 标记实时结论已偏离申请时冻结"
req GET /label-applications/2

step "7. 重新申请（新游标）-> 批准 -> 签发关联旧版的新标签（supersedes_id=1）"
CUR="$(cursor)"
AID="$(req POST /label-applications "{\"batch_id\":\"OUT-2\",\"applicant\":\"qc-zhang\",\"reason\":\"加签 sesame 后重新放行\",\"cursor\":$CUR}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["application_id"])')"
req POST "/label-applications/$AID/approve" '{"reviewer":"qa-li"}' > /dev/null
req POST /batches/OUT-2/labels "{\"application_id\":$AID,\"supersedes_id\":1}"
step "旧标签快照不变（仍是申请时冻结的 gluten+milk），新标签为 gluten+milk+sesame"
req GET /labels/1
req GET "/batches/OUT-2/labels"

step "8. 驳回：驳回后禁止签发，需重新申请"
CUR="$(cursor)"
AID="$(req POST /label-applications "{\"batch_id\":\"OUT-1\",\"applicant\":\"qc-zhang\",\"reason\":\"试放行\",\"cursor\":$CUR}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["application_id"])')"
req POST "/label-applications/$AID/reject" '{"reviewer":"qa-li","note":"放行单不全，驳回"}'
req POST /batches/OUT-1/labels "{\"application_id\":$AID}"

step "9. 封存约束：调查封存期间申请可登记（返回封存状态），但审批 409"
req POST /investigations '{"investigation_id":"INV-100","batch_id":"RM-MILK","reason":"疑似交叉接触"}'
CUR="$(cursor)"
AID="$(req POST /label-applications "{\"batch_id\":\"OUT-2\",\"applicant\":\"qc-zhang\",\"reason\":\"封存期间试申请\",\"cursor\":$CUR}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["application_id"])')"
req POST "/label-applications/$AID/approve" '{"reviewer":"qa-li"}'
step "解除封存后：封存状态已变化，原申请失效（stale），仍需重新申请"
req POST /investigations/INV-100/lift
req POST "/label-applications/$AID/approve" '{"reviewer":"qa-li"}'

step "10. 错误约定：未知批次 400 / 缺少理由 422 / 无效游标 400 / 过期游标 409"
req POST /label-applications '{"batch_id":"NOPE","applicant":"qc","reason":"x","cursor":0}'
req POST /label-applications '{"batch_id":"OUT-2","applicant":"qc","reason":"  ","cursor":0}'
req POST /label-applications '{"batch_id":"OUT-2","applicant":"qc","reason":"x","cursor":99999}'
STALE="$(cursor)"
req POST /materials '{"batch_id":"RM-NEW","allergens":["soy"],"declared_at":"2026-01-03T06:00:00Z"}' > /dev/null
req POST /label-applications "{\"batch_id\":\"OUT-2\",\"applicant\":\"qc\",\"reason\":\"过期游标\",\"cursor\":$STALE}"

step "11. 审计查询：按状态/批次过滤申请列表"
req GET "/label-applications?status=approved"
req GET "/label-applications?batch_id=OUT-2"

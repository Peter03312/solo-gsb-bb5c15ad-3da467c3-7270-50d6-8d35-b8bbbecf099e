#!/usr/bin/env bash
# 过敏原代码并码专项演示：
#   登记并码（旧代码 -> 目标代码，必填原因） -> 串接 -> 代码查询沿革 ->
#   实时溯源按最终规范代码合并同义项/去重（保留每条原始来源路径与声明版本） ->
#   标签快照保留原值 -> 变更事件流 -> 拒绝场景（自映射/别名目标/改指/幂等重试） ->
#   撤销单条并码（凭记录号+必填原因；生效链按仍生效关系解析；列表可查撤销原因/
#   时间；重算含设备残留下游）-> 撤销后重新并码（新记录）-> 撤销使预演凭据失效
# 用法：BASE=http://localhost:8000 ./examples/demo_merges.sh
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

step "1. 登记原料（原始过敏原代码：casein / dairy / milk）与加工产出"
req POST /materials '{"batch_id":"RM-A","allergens":["casein"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-B","allergens":["dairy"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-C","allergens":["milk"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-9","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-A","RM-B","RM-C"],"outputs":["OUT-9"]}'

step "2. 并码前签发标签（审批流：申请 -> 批准 -> 签发；冻结当时结论：三个代码各自独立）"
CUR="$(curl -sS "$BASE/changes/snapshot" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')"
AID="$(req POST /label-applications "{\"batch_id\":\"OUT-9\",\"applicant\":\"qc-demo\",\"reason\":\"例行放行审核\",\"cursor\":$CUR}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["application_id"])')"
req POST "/label-applications/$AID/approve" '{"reviewer":"qa-demo"}' > /dev/null
req POST /batches/OUT-9/labels "{\"application_id\":$AID}"

step "3. 登记并码（必填原因）：casein -> dairy，再串接 dairy -> milk"
req POST /allergen-merges '{"old_code":"casein","target_code":"dairy","reason":"供应商代码勘误：酪蛋白归入乳制品类"}'
req POST /allergen-merges '{"old_code":"dairy","target_code":"milk","reason":"标准 GB-xxxx 统一代码：dairy 并入 milk"}'

step "4. 查询代码：最终规范代码 + 并码沿革（串接链每一跳）"
req GET /allergen-codes/casein
req GET /allergen-codes/milk
req GET /allergen-merges

step "5. 实时溯源：同义项合并为 milk 并去重，保留每条原始来源路径与声明版本；待确认状态不受影响"
req GET /batches/OUT-9/trace

step "6. 已签发标签快照保留原值（casein/dairy/milk 原样不变）"
req GET /labels/1

step "7. 并码事件流（沿用既有字段与全局顺序，仅实际变化批次；op=allergen_code_merge）"
CURSOR=$(curl -sS "$BASE/changes/snapshot" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
req GET "/changes/events?cursor=0&limit=20"

step "8. 拒绝场景：自映射 409；目标当前是别名 409；同一旧代码改指 409；原因空白 422"
req POST /allergen-merges '{"old_code":"milk","target_code":"milk","reason":"x"}'
req POST /allergen-merges '{"old_code":"milk","target_code":"casein","reason":"x"}'
req POST /allergen-merges '{"old_code":"casein","target_code":"milk","reason":"改指其他目标"}'
req POST /allergen-merges '{"old_code":"casein","target_code":"dairy","reason":"   "}'

step "9. 同向重试：完全相同的并码请求幂等返回原登记（200，replayed=true，不重复写/不产事件）"
req POST /allergen-merges '{"old_code":"dairy","target_code":"milk","reason":"重复提交同样请求"}'

step "10. 并码后新原料登记/声明更正按当前并码关系解释（存原始代码，实时出规范代码）"
req POST /materials '{"batch_id":"RM-D","allergens":["casein"],"declared_at":"2026-01-03T06:00:00Z"}'
req GET /batches/RM-D/trace

step "11. 撤销并码（质控复核发现登记有误）：凭并码记录号 + 必填撤销原因撤销单条关系"
# 为撤销演示单独登记一条并码：wheat_old -> wheat（RM-W 声明含 wheat_old）
req POST /materials '{"batch_id":"RM-W","allergens":["wheat_old","wheat"],"declared_at":"2026-01-03T06:00:00Z"}'
req POST /allergen-merges '{"old_code":"wheat_old","target_code":"wheat","reason":"供应商代码勘误"}'
# 用返回的 merge_id（此处新库为 3；实际以上一条响应为准）撤销
req POST /allergen-merges/3/revoke '{"reason":"质控复核：勘误登记有误，撤销该并码"}'

step "12. 撤销后：代码查询仅按生效链给出规范码（wheat_old 恢复为自身），沿革为空"
req GET /allergen-codes/wheat_old
req GET /allergen-merges   # 撤销行保留：status=revoked，带 revoked_reason/revoked_at

step "13. 撤销后同一旧代码可重新并入当前非别名目标（产生新记录，非幂等重试）"
req POST /allergen-merges '{"old_code":"wheat_old","target_code":"wheat","reason":"复核后确认并码，重新登记"}'

step "14. 撤销相关拒绝：重复撤销 409；未知记录 404；生效中改指/别名目标 409（空白原因对生效记录为 422）"
req POST /allergen-merges/3/revoke '{"reason":"重复撤销"}'
req POST /allergen-merges/999/revoke '{"reason":"未知记录"}'
req POST /allergen-merges/4/revoke '{"reason":"   "}'   # 4 仍生效：空白原因 422
req POST /allergen-merges '{"old_code":"dairy","target_code":"casein","reason":"生效链上改指/别名目标"}'

step "15. 撤销事件（op=allergen_code_merge_revocation）追加写入，历史事件不可改写"
req GET "/changes/events?cursor=0&limit=50"

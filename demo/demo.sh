#!/usr/bin/env bash
# 单行校对 API 演示：需要服务已在 http://localhost:8000 运行
#   docker compose up --build -d
#   bash demo/demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"

pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -sS "$BASE/health"; echo

echo
echo "== 1. 建任务：行尾 4，分差 1，5 个候选 =="
echo "   读法 A: 1+2 = 10 分（最高）  读法 B: 3+4 = 9 分  读法 C: 5 = 4 分"
CREATE=$(curl -sS -X POST "$BASE/tasks" \
  -H 'Content-Type: application/json' \
  -d '{
    "line_end": 4,
    "score_gap": 1,
    "candidates": [
      {"id": 1, "start": 0, "end": 2, "text": "天氣", "score": 5},
      {"id": 2, "start": 2, "end": 4, "text": "晴朗", "score": 5},
      {"id": 3, "start": 0, "end": 1, "text": "天",   "score": 3},
      {"id": 4, "start": 1, "end": 4, "text": "氣晴朗", "score": 6},
      {"id": 5, "start": 0, "end": 4, "text": "天氣晴朗", "score": 4}
    ]
  }')
echo "$CREATE" | pp
TASK_ID=$(echo "$CREATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_id"])')
echo ">> task_id=$TASK_ID；读法 1+2 中两词均 needs_review=true（9 分读法在分差 1 内）"

echo
echo "== 2. 确认编号 3（与读法 B 一致的开头词），接受并重算 =="
curl -sS -X POST "$BASE/tasks/$TASK_ID/confirmations" \
  -H 'Content-Type: application/json' -d '{"candidate_id": 3}' | pp

echo
echo "== 3. 再确认编号 1：与已确认的 3 位置重叠 -> conflict，已确认项保留 =="
curl -sS -X POST "$BASE/tasks/$TASK_ID/confirmations" \
  -H 'Content-Type: application/json' -d '{"candidate_id": 1}' | pp

echo
echo "== 4. 重复确认编号 3 -> duplicate，revision 不变 =="
curl -sS -X POST "$BASE/tasks/$TASK_ID/confirmations" \
  -H 'Content-Type: application/json' -d '{"candidate_id": 3}' | pp

echo
echo "== 5. GET 当前结果 =="
curl -sS "$BASE/tasks/$TASK_ID" | pp

echo
echo "== 6. 非法输入演示：候选拼不到行尾 -> 422 =="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"line_end": 5, "score_gap": 0,
       "candidates": [{"id": 1, "start": 0, "end": 4, "text": "abcd", "score": 1}]}'

#!/usr/bin/env python3
"""单行校对 API 演示（标准库实现，无需第三方依赖）。

用法：
    python3 demo/demo.py [base_url]        默认 http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

CORPUS = [
    {"id": 1, "start": 0, "end": 2, "text": "天氣", "score": 5},
    {"id": 2, "start": 2, "end": 4, "text": "晴朗", "score": 5},
    {"id": 3, "start": 0, "end": 1, "text": "天", "score": 3},
    {"id": 4, "start": 1, "end": 4, "text": "氣晴朗", "score": 6},
    {"id": 5, "start": 0, "end": 4, "text": "天氣晴朗", "score": 4},
]


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} ==  HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def create_task(candidates, line_end=4, score_gap=1) -> tuple[int, dict, float]:
    t0 = time.perf_counter()
    status, body = call("POST", "/tasks", {
        "line_end": line_end, "score_gap": score_gap, "candidates": candidates,
    })
    return status, body, time.perf_counter() - t0


def main() -> None:
    show("0. 健康检查", *call("GET", "/health"))

    payload = {"line_end": 4, "score_gap": 1, "candidates": CORPUS}
    status, body = call("POST", "/tasks", payload)
    show("1. 建任务：最高读法 1+2=10，9 分读法 3+4 在分差内，两词待核", status, body)
    assert status == 201
    tid = body["task_id"]

    status, body = call("POST", f"/tasks/{tid}/confirmations", {"candidate_id": 3})
    show("2. 确认 3：accepted，读法重算为 3+4，revision=1", status, body)

    status, body = call("POST", f"/tasks/{tid}/confirmations", {"candidate_id": 1})
    show("3. 确认 1：conflict（与 3 重叠），已确认项保留，revision 仍为 1", status, body)

    status, body = call("POST", f"/tasks/{tid}/confirmations", {"candidate_id": 3})
    show("4. 重复确认 3：duplicate，幂等返回当前结果", status, body)

    status, body = call("GET", f"/tasks/{tid}")
    show("5. 查询当前结果", status, body)

    bad = {
        "line_end": 5, "score_gap": 0,
        "candidates": [{"id": 1, "start": 0, "end": 4, "text": "abcd", "score": 1}],
    }
    status, body = call("POST", "/tasks", bad)
    show("6. 无法拼到行尾的非法任务：422", status, body)

    # 7. 稀疏行：同一组候选，坐标等比例放大 1 / 10^3 / 10^6 倍。
    #    解码只访问有词的位置，遍历量与耗时不随坐标跨度增长。
    print("\n== 7. 稀疏行：坐标等比例放大，建行/确认耗时不随坐标跨度增长 ==")
    for scale in (1, 10**3, 10**6):
        sparse = [dict(c, start=c["start"] * scale, end=c["end"] * scale)
                  for c in CORPUS]
        status, body, t_create = create_task(sparse, line_end=4 * scale)
        assert status == 201, body
        tid = body["task_id"]
        t0 = time.perf_counter()
        status, body2 = call(
            "POST", f"/tasks/{tid}/confirmations", {"candidate_id": 3})
        t_confirm = time.perf_counter() - t0
        assert status == 200 and body2["status"] == "accepted", body2
        ids = [w["id"] for w in body["reading"]["words"]]
        print(f"  放大 10^{len(str(scale))-1} 倍（line_end={4 * scale:>9}）："
              f"建行 {t_create * 1e3:6.2f} ms，确认 {t_confirm * 1e3:6.2f} ms，"
              f"最高读法 {ids}")

    # 8. 并发校对：同一行互斥确认只有一个生效；多行并行确认互不阻塞。
    print("\n== 8. 并发校对 ==")
    status, body, _ = create_task(CORPUS)
    tid = body["task_id"]

    def confirm(task_id: int, cid: int) -> str:
        status, body = call(
            "POST", f"/tasks/{task_id}/confirmations", {"candidate_id": cid})
        assert status == 200, body
        return body["status"]

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(lambda cid: confirm(tid, cid), [1, 3] * 5))
    print(f"  同一行 10 个并发确认（词 1 与词 3 互斥）：{dict(Counter(statuses))}")
    status, body = call("GET", f"/tasks/{tid}")
    print(f"  最终 revision={body['revision']}（只生效一次确认）")

    tids = []
    for _ in range(5):
        status, body, _ = create_task(CORPUS)
        tids.append(body["task_id"])
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(lambda t: confirm(t, 3), tids))
    dt = time.perf_counter() - t0
    print(f"  5 行并行确认：{dict(Counter(statuses))}，总耗时 {dt * 1e3:.1f} ms")


if __name__ == "__main__":
    main()

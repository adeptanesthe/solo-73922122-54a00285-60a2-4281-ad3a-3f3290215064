#!/usr/bin/env python3
"""单行校对 API 演示（标准库实现，无需第三方依赖）。

用法：
    python3 demo/demo.py [base_url]        默认 http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} ==  HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    show("0. 健康检查", *call("GET", "/health"))

    payload = {
        "line_end": 4,
        "score_gap": 1,
        "candidates": [
            {"id": 1, "start": 0, "end": 2, "text": "天氣", "score": 5},
            {"id": 2, "start": 2, "end": 4, "text": "晴朗", "score": 5},
            {"id": 3, "start": 0, "end": 1, "text": "天", "score": 3},
            {"id": 4, "start": 1, "end": 4, "text": "氣晴朗", "score": 6},
            {"id": 5, "start": 0, "end": 4, "text": "天氣晴朗", "score": 4},
        ],
    }
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


if __name__ == "__main__":
    main()

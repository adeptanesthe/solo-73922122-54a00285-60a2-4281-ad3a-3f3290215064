"""真实 HTTP 并发确认测试：起独立 uvicorn 子进程，多线程同时打接口。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter

import httpx
import pytest

pytest.importorskip("uvicorn")


CORPUS = [
    {"id": 1, "start": 0, "end": 2, "text": "天氣", "score": 5},
    {"id": 2, "start": 2, "end": 4, "text": "晴朗", "score": 5},
    {"id": 3, "start": 0, "end": 1, "text": "天", "score": 3},
    {"id": 4, "start": 1, "end": 4, "text": "氣晴朗", "score": 6},
    {"id": 5, "start": 0, "end": 4, "text": "天氣晴朗", "score": 4},
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="proof-http-")
    db_path = os.path.join(tmpdir, "conc.db")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PROOF_DB_PATH=db_path, PYTHONPATH=repo_root)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        try:
            proc.kill()
        except PermissionError:
            pass
        pytest.fail("uvicorn 未能在 15 秒内就绪")
    yield base
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except (PermissionError, subprocess.TimeoutExpired):
        # 某些受限 PID 命名空间中 os.kill 对同用户子进程也会 EPERM；
        # 进程组随测试进程回收，端口用的是临时端口，不影响结果。
        try:
            proc.kill()
        except PermissionError:
            pass


def test_parallel_conflicting_confirms_single_winner(server):
    r = httpx.post(f"{server}/tasks", json={
        "line_end": 4, "score_gap": 1, "candidates": CORPUS,
    }, timeout=10)
    assert r.status_code == 201
    tid = r.json()["task_id"]

    def confirm(cid: int) -> str:
        resp = httpx.post(
            f"{server}/tasks/{tid}/confirmations",
            json={"candidate_id": cid}, timeout=30,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["status"]

    # 词 1 与词 3 位置互斥，各发 5 个并发请求
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(confirm, [1, 3] * 5))

    counts = Counter(statuses)
    assert counts["accepted"] == 1
    assert counts["duplicate"] == 4
    assert counts["conflict"] == 5

    final = httpx.get(f"{server}/tasks/{tid}", timeout=10).json()
    assert final["revision"] == 1  # 只有一次确认真正生效
    confirmed = [c["id"] for c in final["candidates"] if c["confirmed"]]
    assert len(confirmed) == 1
    assert set(confirmed).issubset({1, 3})
    assert final["feasible"] is True

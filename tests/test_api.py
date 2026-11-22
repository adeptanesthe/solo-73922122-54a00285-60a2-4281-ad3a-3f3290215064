"""API 层测试：建任务、查询、确认、重复、冲突、并发确认。"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import engine
from app.main import app

# 行尾 4：
#  A: 1(0-2,5)+2(2-4,5)=10   B: 3(0-1,3)+4(1-4,6)=9   C: 5(0-4,4)=4
CORPUS = [
    {"id": 1, "start": 0, "end": 2, "text": "天氣", "score": 5},
    {"id": 2, "start": 2, "end": 4, "text": "晴朗", "score": 5},
    {"id": 3, "start": 0, "end": 1, "text": "天", "score": 3},
    {"id": 4, "start": 1, "end": 4, "text": "氣晴朗", "score": 6},
    {"id": 5, "start": 0, "end": 4, "text": "天氣晴朗", "score": 4},
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _create(client, gap=1):
    resp = client.post(
        "/tasks", json={"line_end": 4, "score_gap": gap, "candidates": CORPUS}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_returns_best_reading_and_review_flags(client):
    data = _create(client, gap=1)
    assert data["revision"] == 0
    assert data["feasible"] is True
    assert [w["id"] for w in data["reading"]["words"]] == [1, 2]
    assert data["reading"]["total_score"] == 10
    assert {w["id"] for w in data["reading"]["words"] if w["needs_review"]} == {1, 2}
    assert all(c["confirmed"] is False for c in data["candidates"])


def test_gap_zero_marks_nothing(client):
    data = _create(client, gap=0)
    assert all(w["needs_review"] is False for w in data["reading"]["words"])


def test_confirm_accepted_then_recompute(client):
    data = _create(client, gap=1)
    tid = data["task_id"]
    resp = client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["conflict"] is None
    assert body["result"]["revision"] == 1
    assert [w["id"] for w in body["result"]["reading"]["words"]] == [3, 4]
    assert body["result"]["reading"]["total_score"] == 9
    assert {c["id"]: c["confirmed"] for c in body["result"]["candidates"]}[3] is True


def test_duplicate_confirm_is_idempotent(client):
    data = _create(client, gap=1)
    tid = data["task_id"]
    first = client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 3}).json()
    second = client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 3}).json()
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert second["result"]["revision"] == 1  # 版本号不增加
    assert second["result"] == first["result"]


def test_conflicting_confirm_keeps_prior_confirmations(client):
    data = _create(client, gap=1)
    tid = data["task_id"]
    client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 3})
    # 词 1 (0-2) 与已确认的词 3 (0-1) 重叠
    resp = client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 1})
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["conflict"]["reason"] == "overlap"
    assert body["conflict"]["candidate_id"] == 1
    # 当前结果仍是确认 3 之后的读法，已确认项保留
    assert body["result"]["revision"] == 1
    assert [w["id"] for w in body["result"]["reading"]["words"]] == [3, 4]
    assert {c["id"] for c in body["result"]["candidates"] if c["confirmed"]} == {3}
    # GET 与冲突响应里的当前结果一致
    got = client.get(f"/tasks/{tid}").json()
    assert got == body["result"]


def test_unknown_candidate_and_task_404(client):
    data = _create(client)
    tid = data["task_id"]
    assert client.post(
        f"/tasks/{tid}/confirmations", json={"candidate_id": 999}
    ).status_code == 404
    assert client.get("/tasks/999999").status_code == 404


def test_review_flag_lifts_when_alternative_is_gap_away(client):
    # gap=0 时创建：无待核；确认 5（整词 0-4）后也无待核
    data = _create(client, gap=0)
    tid = data["task_id"]
    body = client.post(
        f"/tasks/{tid}/confirmations", json={"candidate_id": 5}
    ).json()
    assert body["status"] == "accepted"
    assert [w["id"] for w in body["result"]["reading"]["words"]] == [5]
    assert body["result"]["reading"]["words"][0]["needs_review"] is False


def test_concurrent_confirms_are_serialized_and_consistent(client):
    data = _create(client, gap=1)
    tid = data["task_id"]
    errors = []
    results = []

    def confirm(cid):
        try:
            r = client.post(
                f"/tasks/{tid}/confirmations", json={"candidate_id": cid}
            ).json()
            results.append((cid, r))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    # 3 与 1 互斥；并发下一个 accepted，另一个 conflict；确认项不丢失
    t1 = threading.Thread(target=confirm, args=(3,))
    t2 = threading.Thread(target=confirm, args=(1,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors
    statuses = {cid: body["status"] for cid, body in results}
    assert sorted(statuses.values()) == ["accepted", "conflict"]

    final = client.get(f"/tasks/{tid}").json()
    confirmed = {c["id"] for c in final["candidates"] if c["confirmed"]}
    assert confirmed == {3} or confirmed == {1}
    assert final["revision"] == 1  # 只有一次确认真正生效


def test_validation_errors(client):
    # 候选数上限 200
    too_many = [
        {"id": i, "start": 0, "end": 1, "text": "x", "score": 1}
        for i in range(201)
    ]
    r = client.post(
        "/tasks", json={"line_end": 1, "score_gap": 0, "candidates": too_many}
    )
    assert r.status_code == 422

    # 无法拼到行尾
    r = client.post("/tasks", json={
        "line_end": 5, "score_gap": 0,
        "candidates": [{"id": 1, "start": 0, "end": 4, "text": "abcd", "score": 1}],
    })
    assert r.status_code == 422

    # 同一词两个分数
    r = client.post("/tasks", json={
        "line_end": 1, "score_gap": 0,
        "candidates": [
            {"id": 1, "start": 0, "end": 1, "text": "a", "score": 1},
            {"id": 2, "start": 0, "end": 1, "text": "a", "score": 2},
        ],
    })
    assert r.status_code == 422


def test_identical_word_duplicates_are_merged(client):
    r = client.post("/tasks", json={
        "line_end": 1, "score_gap": 0,
        "candidates": [
            {"id": 1, "start": 0, "end": 1, "text": "a", "score": 1},
            {"id": 2, "start": 0, "end": 1, "text": "a", "score": 1},
        ],
    })
    assert r.status_code == 201
    assert [c["id"] for c in r.json()["candidates"]] == [1]


def test_line_end_zero_has_empty_reading(client):
    # 空行：没有词可放，唯一合法读法是空读法，总分 0
    r = client.post("/tasks", json={
        "line_end": 0, "score_gap": 0,
        "candidates": [
            {"id": 1, "start": 0, "end": 1, "text": "a", "score": 1},
        ],
    })
    assert r.status_code == 422  # 词的 end 不能超过 line_end=0

    # 没有候选也不允许（min_length=1），所以空行无法建任务，由 422 体现
    r2 = client.post("/tasks", json={
        "line_end": 0, "score_gap": 0, "candidates": [],
    })
    assert r2.status_code == 422


def test_exactly_200_candidates_accepted(client):
    # 构造一条 200 个单位词首尾相接的行，line_end=200
    cands = [
        {"id": i, "start": i - 1, "end": i, "text": f"w{i}", "score": 1}
        for i in range(1, 201)
    ]
    r = client.post("/tasks", json={
        "line_end": 200, "score_gap": 0, "candidates": cands,
    })
    assert r.status_code == 201
    body = r.json()
    assert len(body["candidates"]) == 200
    assert body["reading"]["total_score"] == 200
    assert [w["id"] for w in body["reading"]["words"]] == list(range(1, 201))


# 坐标等比例放大 10**6 倍的稀疏行（与 CORPUS 同一组候选）
SPARSE_SCALE = 10**6
SPARSE_CORPUS = [
    dict(c, start=c["start"] * SPARSE_SCALE, end=c["end"] * SPARSE_SCALE)
    for c in CORPUS
]


def test_sparse_line_create_confirm_get(client):
    # 稀疏行：坐标跨度放大 10**6 倍，建行、确认、查询的解码结果不变
    r = client.post("/tasks", json={
        "line_end": 4 * SPARSE_SCALE, "score_gap": 1, "candidates": SPARSE_CORPUS,
    })
    assert r.status_code == 201, r.text
    data = r.json()
    assert [w["id"] for w in data["reading"]["words"]] == [1, 2]
    assert data["reading"]["total_score"] == 10
    assert {w["id"] for w in data["reading"]["words"] if w["needs_review"]} == {1, 2}

    tid = data["task_id"]
    body = client.post(
        f"/tasks/{tid}/confirmations", json={"candidate_id": 3}
    ).json()
    assert body["status"] == "accepted"
    assert [w["id"] for w in body["result"]["reading"]["words"]] == [3, 4]
    assert client.get(f"/tasks/{tid}").json() == body["result"]


@pytest.fixture
def decode_log(monkeypatch):
    """记录每次完整解码使用的确认集，用于校验同请求不重复解码。"""
    calls = []
    real = engine.compute_result

    def spy(line_end, words, score_gap, confirmed_ids, *args, **kwargs):
        calls.append(frozenset(confirmed_ids))
        return real(line_end, words, score_gap, confirmed_ids, *args, **kwargs)

    monkeypatch.setattr(engine, "compute_result", spy)
    return calls


def test_create_decodes_exactly_once(client, decode_log):
    _create(client, gap=1)
    # 探测可行性的那次解码直接作为响应，不再重算
    assert decode_log == [frozenset()]


def test_confirm_accepted_decodes_once(client, decode_log):
    data = _create(client, gap=1)
    decode_log.clear()
    body = client.post(
        f"/tasks/{data['task_id']}/confirmations", json={"candidate_id": 3}
    ).json()
    assert body["status"] == "accepted"
    # 试算 {3} 的结果直接作为响应：整个请求只完整解码一次
    assert decode_log == [frozenset({3})]


def test_confirm_conflict_decodes_each_input_at_most_once(client, decode_log):
    data = _create(client, gap=1)
    tid = data["task_id"]
    client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 3})
    decode_log.clear()
    body = client.post(
        f"/tasks/{tid}/confirmations", json={"candidate_id": 1}
    ).json()
    assert body["status"] == "conflict"
    # 试算 {1,3} 一次 + 当前结果 {3} 一次：不同确认集各算一次，无重复
    assert sorted(decode_log) == sorted([frozenset({1, 3}), frozenset({3})])
    assert len(decode_log) == len(set(decode_log))


def test_duplicate_confirm_decodes_once(client, decode_log):
    data = _create(client, gap=1)
    tid = data["task_id"]
    client.post(f"/tasks/{tid}/confirmations", json={"candidate_id": 3})
    decode_log.clear()
    body = client.post(
        f"/tasks/{tid}/confirmations", json={"candidate_id": 3}
    ).json()
    assert body["status"] == "duplicate"
    assert decode_log == [frozenset({3})]  # 只算一次当前结果


def test_stale_snapshot_rejudged_against_latest_confirmations(client, monkeypatch):
    # 请求 A 按旧快照试算后、提交前，同一行已有新确认先生效：
    # A 必须在写事务内按最新已确认词重新判断
    data = _create(client, gap=1)
    tid = data["task_id"]

    entered = threading.Event()
    release = threading.Event()
    real = engine.compute_result

    def gated(line_end, words, score_gap, confirmed_ids, *args, **kwargs):
        if frozenset(confirmed_ids) == frozenset({3}):
            entered.set()
            release.wait(timeout=15)  # 卡住 A 对 {3} 的试算
        return real(line_end, words, score_gap, confirmed_ids, *args, **kwargs)

    monkeypatch.setattr(engine, "compute_result", gated)

    reply_a = {}

    def confirm_a():
        reply_a.update(
            client.post(f"/tasks/{tid}/confirmations",
                        json={"candidate_id": 3}).json()
        )

    t = threading.Thread(target=confirm_a)
    t.start()
    try:
        assert entered.wait(timeout=5)  # A 已按旧快照 {} 试算 {3}
        # 同一行的新确认 1 先提交生效
        b = client.post(f"/tasks/{tid}/confirmations",
                        json={"candidate_id": 1}).json()
        assert b["status"] == "accepted"
    finally:
        release.set()
        t.join(timeout=10)

    # A 按最新确认集 {1} 重判：{1,3} 位置重叠 -> 冲突，已确认项保留
    assert reply_a["status"] == "conflict"
    assert reply_a["conflict"]["reason"] == "overlap"
    assert reply_a["result"]["revision"] == 1
    assert {c["id"] for c in reply_a["result"]["candidates"] if c["confirmed"]} == {1}
    assert client.get(f"/tasks/{tid}").json()["revision"] == 1


def test_other_lines_can_confirm_while_one_line_recomputes(client, monkeypatch):
    # 模拟一次耗时的重算：重算在写事务外进行，不阻塞其他行的确认
    heavy = client.post("/tasks", json={
        "line_end": 8, "score_gap": 1,
        "candidates": [
            {"id": 1, "start": 0, "end": 4, "text": "甲乙", "score": 5},
            {"id": 2, "start": 4, "end": 8, "text": "丙丁", "score": 5},
            {"id": 3, "start": 0, "end": 8, "text": "甲乙丙丁", "score": 4},
        ],
    }).json()
    light = _create(client, gap=1)

    entered = threading.Event()   # 重行重算已开始
    release = threading.Event()   # 放行重行重算
    real = engine.compute_result

    def gated(line_end, words, score_gap, confirmed_ids, *args, **kwargs):
        if line_end == 8:
            entered.set()
            release.wait(timeout=15)  # 卡住重行的重算
        return real(line_end, words, score_gap, confirmed_ids, *args, **kwargs)

    monkeypatch.setattr(engine, "compute_result", gated)

    heavy_reply = {}

    def confirm_heavy():
        heavy_reply.update(
            client.post(
                f"/tasks/{heavy['task_id']}/confirmations",
                json={"candidate_id": 3},
            ).json()
        )

    t = threading.Thread(target=confirm_heavy)
    t.start()
    try:
        assert entered.wait(timeout=5)  # 重行进入重算（在写事务外）
        t0 = time.monotonic()
        light_reply = client.post(
            f"/tasks/{light['task_id']}/confirmations",
            json={"candidate_id": 3},
        ).json()
        elapsed = time.monotonic() - t0
        # 重行还在重算，轻行的确认已经完成（重算若持写锁，此处会卡 15s）
        assert light_reply["status"] == "accepted"
        assert elapsed < 5
    finally:
        release.set()
        t.join(timeout=10)
    assert heavy_reply["status"] == "accepted"
    assert heavy_reply["result"]["revision"] == 1

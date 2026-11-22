"""FastAPI 单行校对 API。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Dict, FrozenSet, List, Optional, Set

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db, engine
from .models import (
    CandidateOut,
    ConfirmIn,
    ConfirmResponse,
    Conflict,
    Reading,
    ReadingWord,
    TaskIn,
    TaskResult,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_path = os.environ.get("PROOF_DB_PATH", "/data/proofreading.db")
    conn = db.connect(app.state.db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="古籍 OCR 单行校对 API",
    version="1.0.0",
    description="接收一行 OCR 的候选词，按首尾相接约束求总分最高的合法读法，"
    "并依据分差标出需要回看原图的词；支持逐词确认与冲突/并发处理。",
    lifespan=lifespan,
)


@app.exception_handler(engine.ValidationError)
async def validation_error_handler(request: Request, exc: engine.ValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.detail})


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _load_words(conn, task_id: int) -> List[engine.Word]:
    rows = db.get_candidates(conn, task_id)
    return [
        engine.Word(
            id=r["candidate_id"],
            start=r["start_pos"],
            end=r["end_pos"],
            text=r["text"],
            score=r["score"],
        )
        for r in rows
    ]


def _assemble_result(
    task_id: int,
    line_end: int,
    score_gap: int,
    revision: int,
    words: List[engine.Word],
    confirmed_ids: Set[int],
    result: engine.Result,
) -> TaskResult:
    """把一次已完成的解码组装成响应；本身不再解码。"""
    candidates = [
        CandidateOut(
            id=w.id, start=w.start, end=w.end, text=w.text, score=w.score,
            confirmed=w.id in confirmed_ids,
        )
        for w in words
    ]

    reading = None
    if result.feasible:
        by_id = {w.id: w for w in words}
        reading = Reading(
            total_score=result.total_score,
            words=[
                ReadingWord(
                    id=cid,
                    start=by_id[cid].start,
                    end=by_id[cid].end,
                    text=by_id[cid].text,
                    score=by_id[cid].score,
                    needs_review=cid in result.review_ids,
                )
                for cid in result.ordered_ids
            ],
        )

    return TaskResult(
        task_id=task_id,
        line_end=line_end,
        score_gap=score_gap,
        revision=revision,
        candidates=candidates,
        reading=reading,
        feasible=result.feasible,
    )


# 一次请求内的解码记忆：键是已生效确认集。同一请求中“候选相同 +
# 确认项相同”的完整解码最多做一次（含并发交错下的边界情形）。
DecodeMemo = Dict[FrozenSet[int], engine.Result]


def _build_result(
    conn, task_id: int, memo: Optional[DecodeMemo] = None
) -> TaskResult:
    task = db.get_task(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    words = _load_words(conn, task_id)
    confirmed_ids = db.get_confirmation_ids(conn, task_id)

    key = frozenset(confirmed_ids)
    result = memo.get(key) if memo is not None else None
    if result is None:
        result = engine.compute_result(
            task["line_end"], words, task["score_gap"], confirmed_ids
        )
        if memo is not None:
            memo[key] = result

    return _assemble_result(
        task_id,
        task["line_end"],
        task["score_gap"],
        task["revision"],
        words,
        confirmed_ids,
        result,
    )


@app.post("/tasks", response_model=TaskResult, status_code=201)
def create_task(payload: TaskIn, conn=Depends(get_conn)):
    """建任务：校验候选、落库、计算并返回首个读法与待核标记。"""
    raw = [c.model_dump() for c in payload.candidates]
    words = engine.normalize_candidates(payload.line_end, raw)

    # 建任务前先确认存在合法读法，避免产生无法校对的死任务；
    # 这次完整解码同时就是响应所需的结果，同一请求不再重复解码
    probe = engine.compute_result(
        payload.line_end, words, payload.score_gap, set()
    )
    if not probe.feasible:
        raise HTTPException(
            status_code=422,
            detail=f"候选无法从位置 0 首尾相接到行尾 {payload.line_end}："
            f"{probe.conflict_detail}",
        )

    task_id = db.insert_task(
        conn,
        payload.line_end,
        payload.score_gap,
        [
            {"id": w.id, "start": w.start, "end": w.end, "text": w.text,
             "score": w.score}
            for w in words
        ],
    )
    return _assemble_result(
        task_id, payload.line_end, payload.score_gap, 0, words, set(), probe
    )


@app.get("/tasks/{task_id}", response_model=TaskResult)
def get_task(task_id: int, conn=Depends(get_conn)):
    """查询任务当前结果（基于全部已生效确认项）。"""
    return _build_result(conn, task_id)


@app.post("/tasks/{task_id}/confirmations", response_model=ConfirmResponse)
def confirm_word(task_id: int, payload: ConfirmIn, conn=Depends(get_conn)):
    """按编号确认一个词。

    - 重复确认：幂等返回当前结果（status=duplicate，revision 不变）；
    - 能与已生效确认项拼成整行：接受、写入、重算读法与待核标记
      （status=accepted，revision +1）；
    - 拼不成整行：保留已确认项，返回 conflict 和当前结果。

    耗时的完整解码在不持写锁的情况下进行，重算读法和待核词期间其他
    行的确认可以照常提交；随后在一个很短的 IMMEDIATE 事务内重新读取
    最新已生效确认集再裁决——若同一行有新的确认先提交，本次请求按
    最新已确认词判断：能拼满整行就接受，拼不满则返回冲突，重复确认
    按原规则幂等处理。
    """
    memo: DecodeMemo = {}

    # —— 相位 1：无锁读取快照，昂贵的试算在写事务之外完成 ——
    task = db.get_task(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    words = _load_words(conn, task_id)
    if payload.candidate_id not in {w.id for w in words}:
        raise HTTPException(
            status_code=404,
            detail=f"任务 {task_id} 中没有编号为 {payload.candidate_id} 的候选",
        )
    snapshot = db.get_confirmation_ids(conn, task_id)
    if payload.candidate_id not in snapshot:
        # 确认集只增不减：快照里还没有，才可能不是重复确认，先按快照试算
        trial_set = snapshot | {payload.candidate_id}
        memo[frozenset(trial_set)] = engine.compute_result(
            task["line_end"], words, task["score_gap"], trial_set
        )

    # —— 相位 2：短写事务，按最新已生效确认集裁决 ——
    db.begin_write(conn)
    finished = False  # 事务已 commit 或 rollback
    try:
        current = db.get_confirmation_ids(conn, task_id)
        if payload.candidate_id in current:
            db.rollback(conn)  # 重复确认不产生写入
            finished = True
            return ConfirmResponse(
                status="duplicate",
                result=_build_result(conn, task_id, memo),
                conflict=None,
            )

        trial_set = current | {payload.candidate_id}
        trial = memo.get(frozenset(trial_set))
        if trial is None:
            # 同一行有新的确认先提交：按最新确认集重新试算
            trial = engine.compute_result(
                task["line_end"], words, task["score_gap"], trial_set
            )
            memo[frozenset(trial_set)] = trial

        if not trial.feasible:
            db.rollback(conn)  # 保留已确认项，拒绝本次确认
            finished = True
            return ConfirmResponse(
                status="conflict",
                result=_build_result(conn, task_id, memo),
                conflict=Conflict(
                    reason=trial.conflict_reason or "no_full_tiling",
                    candidate_id=payload.candidate_id,
                    detail=trial.conflict_detail or "加入该确认后无法拼成整行",
                ),
            )

        db.add_confirmation(conn, task_id, payload.candidate_id)
        new_revision = db.get_task(conn, task_id)["revision"]
        db.commit(conn)
        finished = True
    finally:
        if not finished:
            db.rollback(conn)

    # 接受路径：响应就是刚提交的试算结果，直接组装，不再重算
    return ConfirmResponse(
        status="accepted",
        result=_assemble_result(
            task_id,
            task["line_end"],
            task["score_gap"],
            new_revision,
            words,
            trial_set,
            trial,
        ),
        conflict=None,
    )


@app.get("/health")
def health():
    return {"status": "ok"}

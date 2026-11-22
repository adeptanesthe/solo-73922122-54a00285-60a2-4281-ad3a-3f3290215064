"""FastAPI 单行校对 API。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

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


def _build_result(conn, task_id: int) -> TaskResult:
    task = db.get_task(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    rows = db.get_candidates(conn, task_id)
    words = [
        engine.Word(
            id=r["candidate_id"],
            start=r["start_pos"],
            end=r["end_pos"],
            text=r["text"],
            score=r["score"],
        )
        for r in rows
    ]
    confirmed_ids = db.get_confirmation_ids(conn, task_id)

    result = engine.compute_result(
        task["line_end"], words, task["score_gap"], confirmed_ids
    )

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
        line_end=task["line_end"],
        score_gap=task["score_gap"],
        revision=task["revision"],
        candidates=candidates,
        reading=reading,
        feasible=result.feasible,
    )


@app.post("/tasks", response_model=TaskResult, status_code=201)
def create_task(payload: TaskIn, conn=Depends(get_conn)):
    """建任务：校验候选、落库、计算并返回首个读法与待核标记。"""
    raw = [c.model_dump() for c in payload.candidates]
    words = engine.normalize_candidates(payload.line_end, raw)

    # 建任务前先确认存在合法读法，避免产生无法校对的死任务
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
    return _build_result(conn, task_id)


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
    旧结果上的确认与并发确认都统一在写事务内按当前已生效确认项校验。
    """
    db.begin_write(conn)
    finished = False  # 事务已 commit 或 rollback
    try:
        task = db.get_task(conn, task_id)
        if task is None:
            db.rollback(conn)
            finished = True
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        rows = db.get_candidates(conn, task_id)
        candidate_ids = {r["candidate_id"] for r in rows}
        if payload.candidate_id not in candidate_ids:
            db.rollback(conn)
            finished = True
            raise HTTPException(
                status_code=404,
                detail=f"任务 {task_id} 中没有编号为 {payload.candidate_id} 的候选",
            )

        current = db.get_confirmation_ids(conn, task_id)
        if payload.candidate_id in current:
            db.rollback(conn)  # 重复确认不产生写入
            finished = True
            return ConfirmResponse(
                status="duplicate",
                result=_build_result(conn, task_id),
                conflict=None,
            )

        words = [
            engine.Word(
                id=r["candidate_id"], start=r["start_pos"], end=r["end_pos"],
                text=r["text"], score=r["score"],
            )
            for r in rows
        ]
        trial = engine.compute_result(
            task["line_end"],
            words,
            task["score_gap"],
            current | {payload.candidate_id},
        )
        if not trial.feasible:
            db.rollback(conn)  # 保留已确认项，拒绝本次确认
            finished = True
            return ConfirmResponse(
                status="conflict",
                result=_build_result(conn, task_id),
                conflict=Conflict(
                    reason=trial.conflict_reason or "no_full_tiling",
                    candidate_id=payload.candidate_id,
                    detail=trial.conflict_detail or "加入该确认后无法拼成整行",
                ),
            )

        db.add_confirmation(conn, task_id, payload.candidate_id)
        db.commit(conn)
        finished = True
    finally:
        if not finished:
            db.rollback(conn)

    return ConfirmResponse(
        status="accepted", result=_build_result(conn, task_id), conflict=None
    )


@app.get("/health")
def health():
    return {"status": "ok"}

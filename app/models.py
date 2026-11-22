"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class CandidateIn(BaseModel):
    id: int = Field(..., description="候选编号，由调用方给出，同一任务内唯一")
    start: int = Field(..., ge=0, description="词在整行中的起始位置（含）")
    end: int = Field(..., ge=0, description="词在整行中的结束位置（不含）")
    text: str = Field(..., min_length=1, description="OCR 候选文字")
    score: int = Field(..., description="整数分数，允许为负")


class TaskIn(BaseModel):
    line_end: int = Field(..., ge=0, description="行尾位置，合法读法须从 0 首尾相接到此位置")
    score_gap: int = Field(..., ge=0, description="非负整数分差，距最高分不超过该分差的读法都参与待核判定")
    candidates: List[CandidateIn] = Field(..., min_length=1, max_length=200)


class CandidateOut(BaseModel):
    id: int
    start: int
    end: int
    text: str
    score: int
    confirmed: bool


class ReadingWord(BaseModel):
    id: int
    start: int
    end: int
    text: str
    score: int
    needs_review: bool = Field(
        ..., description="是否待核：存在一条距最高分不超过分差的合法读法不含此词"
    )


class Reading(BaseModel):
    total_score: int
    words: List[ReadingWord]


class TaskResult(BaseModel):
    task_id: int
    line_end: int
    score_gap: int
    revision: int = Field(..., description="已生效确认的版本号，每次接受确认后递增")
    candidates: List[CandidateOut]
    reading: Reading | None = Field(None, description="当前总分最高的合法读法；无合法读法时为 null")
    feasible: bool


class ConfirmIn(BaseModel):
    candidate_id: int = Field(..., description="要确认的候选编号")


class Conflict(BaseModel):
    reason: str
    candidate_id: int | None = None
    detail: str


class ConfirmResponse(BaseModel):
    status: str = Field(..., description="accepted / duplicate / conflict")
    result: TaskResult
    conflict: Conflict | None = None

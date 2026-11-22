"""SQLite 持久化层。

- WAL 模式 + busy_timeout，保证读写并发下的正确性；
- 所有写操作在一个 IMMEDIATE 事务内完成，确认以“已生效的确认项”为准校验，
  并发确认由数据库写锁串行化，不会出现丢失更新。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional, Sequence, Set, Tuple

DB_PATH = os.environ.get("PROOF_DB_PATH", "/data/proofreading.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_end INTEGER NOT NULL,
    score_gap INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS candidates (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    candidate_id INTEGER NOT NULL,
    start_pos INTEGER NOT NULL,
    end_pos INTEGER NOT NULL,
    text TEXT NOT NULL,
    score INTEGER NOT NULL,
    PRIMARY KEY (task_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS confirmations (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    candidate_id INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, candidate_id)
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # check_same_thread=False：FastAPI 同步依赖的收尾可能切换工作线程；
    # 每个请求独占一个连接，WAL + IMMEDIATE 事务保证并发正确性。
    conn = sqlite3.connect(
        path, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def insert_task(
    conn: sqlite3.Connection,
    line_end: int,
    score_gap: int,
    words: Sequence[dict],
) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO tasks(line_end, score_gap) VALUES (?, ?)",
            (line_end, score_gap),
        )
        task_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO candidates(task_id, candidate_id, start_pos, end_pos, "
            "text, score) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (task_id, w["id"], w["start"], w["end"], w["text"], w["score"])
                for w in words
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return task_id


def get_task(conn: sqlite3.Connection, task_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,)
    ).fetchone()


def get_candidates(conn: sqlite3.Connection, task_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT candidate_id, start_pos, end_pos, text, score "
        "FROM candidates WHERE task_id=? ORDER BY rowid",
        (task_id,),
    ).fetchall()


def get_confirmation_ids(conn: sqlite3.Connection, task_id: int) -> Set[int]:
    rows = conn.execute(
        "SELECT candidate_id FROM confirmations WHERE task_id=?", (task_id,)
    ).fetchall()
    return {r["candidate_id"] for r in rows}


def add_confirmation(
    conn: sqlite3.Connection, task_id: int, candidate_id: int
) -> None:
    """在已开启的 IMMEDIATE 事务内追加确认并递增版本号。"""
    conn.execute(
        "INSERT OR IGNORE INTO confirmations(task_id, candidate_id) VALUES (?, ?)",
        (task_id, candidate_id),
    )
    conn.execute(
        "UPDATE tasks SET revision=revision+1 WHERE id=?", (task_id,)
    )


def begin_write(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def rollback(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")

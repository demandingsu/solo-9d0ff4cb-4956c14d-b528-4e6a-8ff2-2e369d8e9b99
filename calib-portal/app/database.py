"""SQLite 持久化：批次与逐行进度。

进度按 (门户地址, 设备编号, 校准日期) 唯一持久化，
浏览器关闭、服务重启、重新上传同一 CSV 都可以续办。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    portal_url TEXT NOT NULL,
    device_no TEXT NOT NULL,
    cal_date TEXT NOT NULL,
    indication_error TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '',
    portal_record_url TEXT NOT NULL DEFAULT '',
    portal_record_id TEXT NOT NULL DEFAULT '',
    screenshot TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(portal_url, device_no, cal_date)
);
CREATE INDEX IF NOT EXISTS idx_rows_batch ON rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_rows_status ON rows(status);
"""


def now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def create_batch(portal_url: str, filename: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO batches(portal_url, filename, status, created_at,"
            " updated_at) VALUES (?, ?, 'running', ?, ?)",
            (portal_url, filename, now(), now()),
        )
        return int(cur.lastrowid)


def get_batch(batch_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()


def list_batches() -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
        )


def set_batch_status(batch_id: int, status: str, note: str | None = None) -> None:
    with conn() as c:
        if note is None:
            c.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE id=?",
                (status, now(), batch_id),
            )
        else:
            c.execute(
                "UPDATE batches SET status=?, note=?, updated_at=? WHERE id=?",
                (status, note, now(), batch_id),
            )


def upsert_row(
    batch_id: int,
    portal_url: str,
    device_no: str,
    cal_date: str,
    indication_error: str,
    line_no: int,
) -> tuple[int, str]:
    """插入一行；若 (门户, 设备, 日期) 已存在则复用原进度。

    返回 (row_id, action)，action 为 'created' / 'reused' / 'dup_in_csv'。
    """
    with conn() as c:
        existing = c.execute(
            "SELECT * FROM rows WHERE portal_url=? AND device_no=? AND cal_date=?",
            (portal_url, device_no, cal_date),
        ).fetchone()
        if existing is not None:
            if existing["batch_id"] == batch_id:
                # 同一个 CSV 文件内出现重复键
                return existing["id"], "dup_in_csv"
            return existing["id"], "reused"
        cur = c.execute(
            "INSERT INTO rows(batch_id, portal_url, device_no, cal_date,"
            " indication_error, line_no, status, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (batch_id, portal_url, device_no, cal_date,
             indication_error, line_no, now()),
        )
        return int(cur.lastrowid), "created"


def list_rows(batch_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM rows WHERE batch_id=? ORDER BY line_no, id",
                (batch_id,),
            ).fetchall()
        )


def get_row(row_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM rows WHERE id=?", (row_id,)).fetchone()


def update_row(row_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [row_id]
    with conn() as c:
        c.execute(f"UPDATE rows SET {cols} WHERE id=?", vals)


def increment_attempt(row_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE rows SET attempts=attempts+1, updated_at=? WHERE id=?",
            (now(), row_id),
        )


def rows_by_status(batch_id: int, statuses: set[str]) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in statuses)
    with conn() as c:
        return list(
            c.execute(
                f"SELECT * FROM rows WHERE batch_id=? AND status IN ({marks})"
                " ORDER BY line_no, id",
                (batch_id, *statuses),
            ).fetchall()
        )


def batch_counts(batch_id: int) -> dict[str, int]:
    with conn() as c:
        rs = c.execute(
            "SELECT status, COUNT(*) AS n FROM rows WHERE batch_id=?"
            " GROUP BY status",
            (batch_id,),
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rs}
    counts["total"] = sum(counts.values())
    return counts


def in_flight_rows() -> list[sqlite3.Row]:
    """服务重启时：停留在 querying（在途）的行。

    暂停态是人工待办，不会被自动反复重查；
    人工"重新查询"会把行置回 querying，从而获得一次性只查不提复核。
    """
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM rows WHERE status='querying'"
                " ORDER BY batch_id, line_no, id"
            ).fetchall()
        )


def running_batches() -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM batches WHERE status='running' ORDER BY id"
            ).fetchall()
        )

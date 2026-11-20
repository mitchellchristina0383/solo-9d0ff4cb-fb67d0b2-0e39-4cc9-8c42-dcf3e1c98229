"""SQLite 持久化与表结构。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DB = os.environ.get("PRINTROOM_DB", "data/printroom.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('draft', 'ready')),
    page_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    finalized_at TEXT
);

CREATE TABLE IF NOT EXISTS document_pages (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL,
    PRIMARY KEY (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL UNIQUE REFERENCES documents(id),
    status        TEXT NOT NULL CHECK (status IN ('queued', 'printing', 'jammed', 'stopped', 'completed')),
    page_count    INTEGER NOT NULL,
    total_sheets  INTEGER NOT NULL,
    next_sheet    INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT
);

-- 一段打印任务：初始派单，或一次卡纸停稳后派出的续单。
CREATE TABLE IF NOT EXISTS print_segments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id),
    printer_id       TEXT NOT NULL REFERENCES printers(id),
    kind             TEXT NOT NULL CHECK (kind IN ('initial', 'continuation')),
    start_sheet      INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active', 'jammed', 'stopped', 'completed')),
    continued_from   INTEGER UNIQUE REFERENCES print_segments(id),
    created_at       TEXT NOT NULL,
    ended_at         TEXT,
    note             TEXT
);

-- 每一张纸的一次打印尝试。半张卡纸会留下 scrapped 记录，续印会新建一条尝试。
CREATE TABLE IF NOT EXISTS sheet_attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id),
    segment_id       INTEGER NOT NULL REFERENCES print_segments(id),
    sheet_no         INTEGER NOT NULL,
    printer_id       TEXT NOT NULL REFERENCES printers(id),
    front_page       INTEGER NOT NULL,
    back_page        INTEGER,
    required_back    INTEGER NOT NULL CHECK (required_back IN (0, 1)),
    front_printed    INTEGER NOT NULL DEFAULT 0 CHECK (front_printed IN (0, 1)),
    back_printed     INTEGER NOT NULL DEFAULT 0 CHECK (back_printed IN (0, 1)),
    status           TEXT NOT NULL CHECK (status IN ('in_progress', 'complete', 'scrapped')),
    scrap_reason     TEXT,
    started_at       TEXT NOT NULL,
    front_printed_at TEXT,
    back_printed_at  TEXT,
    ended_at         TEXT
);

-- 每台打印机同时只能有一段活动打印段，或一段待停稳的卡纸段。
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_printer
    ON print_segments(printer_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_jammed_printer
    ON print_segments(printer_id) WHERE status = 'jammed';

-- 同一作业的同一张纸只允许有一条“完整出纸”记录，防止重复计数。
CREATE UNIQUE INDEX IF NOT EXISTS ux_complete_sheet
    ON sheet_attempts(job_id, sheet_no) WHERE status = 'complete';

-- 同一作业同时只有一张正在出纸的纸。
CREATE UNIQUE INDEX IF NOT EXISTS ux_in_progress_attempt
    ON sheet_attempts(job_id) WHERE status = 'in_progress';

CREATE INDEX IF NOT EXISTS ix_segments_job ON print_segments(job_id);
CREATE INDEX IF NOT EXISTS ix_attempts_job ON sheet_attempts(job_id);
CREATE INDEX IF NOT EXISTS ix_attempts_segment ON sheet_attempts(segment_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=20.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO printers(id, name, position) VALUES (?, ?, ?)",
            [("P1", "一号机", 1), ("P2", "二号机", 2)],
        )
        conn.execute("PRAGMA user_version = 1")
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """写事务；BEGIN IMMEDIATE 让并发请求串行化。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def reading(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()

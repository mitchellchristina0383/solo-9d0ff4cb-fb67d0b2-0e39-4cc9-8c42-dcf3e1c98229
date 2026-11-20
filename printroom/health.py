"""存储健康探测：以“当前 SQLite 作业库能否实际提交一次写入”为准。

判定规则：
- 探测只用 ``mode=rw`` 打开现有库文件，绝不新建空库；
- 每次探测在专用表 ``health_probe`` 里真实提交一次写入并回读校验，
  不触碰 documents / jobs / print_segments / sheet_attempts 等业务表；
- 出纸事务占锁时 1 秒内报“暂忙”，沿用上次已确认的就绪判定，
  尚无结论则保持未就绪；
- 卷只读、库文件缺失或无法打开都报故障；故障后必须重新实际提交
  一次探测写入才恢复就绪，无需重启服务。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

# 占锁时探测最多等这么久就报“暂忙”，保证 /healthz 在 1 秒内出结论。
BUSY_TIMEOUT_SECONDS = 0.8

# 与 db.SCHEMA 中的定义保持一致；探测里再执行一次 IF NOT EXISTS，
# 让旧版本创建的库在首次探测时自愈，无需手工迁移。
PROBE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS health_probe (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    touched_at TEXT NOT NULL
)
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _primary_code(exc: sqlite3.Error) -> Optional[int]:
    """取 SQLite 主错误码（去掉扩展位），3.11+ 的异常对象自带。"""
    code = getattr(exc, "sqlite_errorcode", None)
    return None if code is None else code & 0xFF


class HealthProbe:
    """对同一个作业库做串行化写探测，并记住最近一次已确认的就绪判定。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_ready: Optional[bool] = None  # None = 尚无已确认的结论
        self._last_ready_at: Optional[str] = None

    def check(self) -> dict[str, Any]:
        # 串行探测：两个探测互不重叠，否则彼此会把对方误判成“占锁”。
        with self._lock:
            try:
                return self._probe()
            except Exception as exc:  # 探测本身不应把接口打成 500
                return self._failure(f"探测异常：{exc!r}", "probe_error")

    # ---------------------------------------------------------------- 内部
    def _probe(self) -> dict[str, Any]:
        path = self.db_path
        if not os.path.exists(path):
            return self._failure("库文件缺失", "db_missing")
        # mode=rw：文件不存在时 SQLite 直接报错，不会新建空库。
        uri = "file:%s?mode=rw" % urllib.parse.quote(os.path.abspath(path))
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(
                uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None
            )
            stamp = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(PROBE_TABLE_DDL)
            conn.execute(
                "INSERT OR REPLACE INTO health_probe(id, touched_at) VALUES (1, ?)",
                (stamp,),
            )
            conn.execute("COMMIT")
            row = conn.execute(
                "SELECT touched_at FROM health_probe WHERE id = 1"
            ).fetchone()
            if row is None or row[0] != stamp:
                return self._failure("探测写入未生效", "probe_not_applied")
            return self._ready()
        except sqlite3.OperationalError as exc:
            code = _primary_code(exc)
            if code == sqlite3.SQLITE_BUSY:
                return self._busy()
            if code == sqlite3.SQLITE_READONLY:
                return self._failure("卷只读，探测写入被驳回", "readonly")
            if code == sqlite3.SQLITE_CANTOPEN:
                return self._failure("库文件无法打开（权限或挂载异常）", "db_unopenable")
            return self._failure(f"探测写入失败：{exc}", "io_error")
        except sqlite3.Error as exc:
            return self._failure(f"探测写入失败：{exc}", "io_error")
        finally:
            if conn is not None:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                conn.close()

    def _ready(self) -> dict[str, Any]:
        self._last_ready = True
        self._last_ready_at = _now_iso()
        return self._report("ready", True, "探测写入已提交", "ok")

    def _busy(self) -> dict[str, Any]:
        verdict = self._last_ready
        if verdict is None:
            detail = "出纸事务占锁，1 秒内未获得写锁；尚无已确认的就绪判定，保持未就绪"
        elif verdict:
            detail = "出纸事务占锁，1 秒内未获得写锁；沿用上次已确认判定：就绪"
        else:
            detail = "出纸事务占锁，1 秒内未获得写锁；沿用上次已确认判定：故障"
        # 占锁不是故障，但也不重新确认：沿用旧结论，没有结论就是未就绪。
        return self._report("busy", bool(verdict), detail, "busy")

    def _failure(self, detail: str, code: str) -> dict[str, Any]:
        self._last_ready = False
        return self._report("error", False, detail, code)

    def _report(self, status: str, ok: bool, detail: str, code: str) -> dict[str, Any]:
        return {
            "ok": ok,
            "status": status,  # ready | busy | error
            "code": code,
            "writable": ok,
            "detail": detail,
            "db_path": self.db_path,
            "checked_at": _now_iso(),
            "last_ready_at": self._last_ready_at,
        }

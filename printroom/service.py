"""打印队列领域逻辑。

纸张编号从 1 开始：第 s 张纸正面是第 2s-1 页，背面是第 2s 页。
奇数页文档的最后一张纸只有正面，背面留白。

状态机：
    queued --start--> printing --卡纸--> jammed --确认停稳--> stopped
    stopped --换机派续单--> printing（备机从首张未完整出纸处续印）
    printing --最后一张完整出纸--> completed
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from . import db

MAX_TITLE = 200
MAX_PAGE_CONTENT = 20000

STATUS_LABELS = {
    "draft": "草稿",
    "ready": "已入队",
    "queued": "排队中",
    "printing": "打印中",
    "jammed": "卡纸待停稳",
    "stopped": "已停稳待换机",
    "completed": "已完成",
    "active": "打印中",
    "complete": "完整出纸",
    "scrapped": "作废",
    "in_progress": "出纸中",
    "initial": "初始派单",
    "continuation": "换机续单",
}


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 409, code: str = "conflict"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _require(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> sqlite3.Row:
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ServiceError("记录不存在", status=404, code="not_found")
    return row


def _get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    return _require(conn, "SELECT * FROM documents WHERE id = ?", (document_id,))


def _get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return _require(conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))


def _current_segment(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM print_segments WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def _active_segment(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM print_segments WHERE job_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def _in_progress_attempt(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sheet_attempts WHERE job_id = ? AND status = 'in_progress' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def _printer_is_free(conn: sqlite3.Connection, printer_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM print_segments WHERE printer_id = ? "
        "AND status IN ('active', 'jammed') LIMIT 1",
        (printer_id,),
    ).fetchone()
    return row is None


def _require_printer(conn: sqlite3.Connection, printer_id: str) -> sqlite3.Row:
    return _require(conn, "SELECT * FROM printers WHERE id = ?", (printer_id,))


def _new_attempt(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    segment: sqlite3.Row,
    sheet_no: int,
) -> None:
    front_page = 2 * sheet_no - 1
    back_page = 2 * sheet_no if 2 * sheet_no <= job["page_count"] else None
    conn.execute(
        """
        INSERT INTO sheet_attempts
            (job_id, segment_id, sheet_no, printer_id, front_page, back_page,
             required_back, status, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
        """,
        (
            job["id"],
            segment["id"],
            sheet_no,
            segment["printer_id"],
            front_page,
            back_page,
            1 if back_page is not None else 0,
            now_iso(),
        ),
    )


def _advance_after_sheet(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    job = _get_job(conn, job_id)
    segment = _current_segment(conn, job_id)
    next_sheet = job["next_sheet"] + 1
    if next_sheet > job["total_sheets"]:
        ts = now_iso()
        conn.execute(
            "UPDATE jobs SET status = 'completed', next_sheet = ?, completed_at = ? WHERE id = ?",
            (next_sheet, ts, job_id),
        )
        conn.execute(
            "UPDATE print_segments SET status = 'completed', ended_at = ? WHERE id = ?",
            (ts, segment["id"]),
        )
    else:
        conn.execute("UPDATE jobs SET next_sheet = ? WHERE id = ?", (next_sheet, job_id))
        _new_attempt(conn, _get_job(conn, job_id), segment, next_sheet)
    return _get_job(conn, job_id)


# --------------------------------------------------------------------------
# 文档录入
# --------------------------------------------------------------------------


def create_document(db_path: str, title: str) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ServiceError("请填写文档标题", status=400, code="invalid_title")
    if len(title) > MAX_TITLE:
        raise ServiceError("文档标题过长", status=400, code="invalid_title")
    with db.transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO documents(title, status, page_count, created_at) VALUES (?, 'draft', 0, ?)",
            (title, now_iso()),
        )
        return _document_dict(conn, cur.lastrowid)


def add_page(db_path: str, document_id: int, content: str = "") -> dict[str, Any]:
    content = content or ""
    if len(content) > MAX_PAGE_CONTENT:
        raise ServiceError("页面内容过长", status=400, code="invalid_page")
    with db.transaction(db_path) as conn:
        doc = _get_document(conn, document_id)
        if doc["status"] != "draft":
            raise ServiceError("文档已入队，不能再修改页面")
        page_no = doc["page_count"] + 1
        conn.execute(
            "INSERT INTO document_pages(document_id, page_no, content, added_at) "
            "VALUES (?, ?, ?, ?)",
            (document_id, page_no, content, now_iso()),
        )
        conn.execute(
            "UPDATE documents SET page_count = ? WHERE id = ?", (page_no, document_id)
        )
        return _document_dict(conn, document_id)


def delete_last_page(db_path: str, document_id: int) -> dict[str, Any]:
    with db.transaction(db_path) as conn:
        doc = _get_document(conn, document_id)
        if doc["status"] != "draft":
            raise ServiceError("文档已入队，不能再修改页面")
        if doc["page_count"] == 0:
            raise ServiceError("文档还没有页面", status=400, code="empty_document")
        conn.execute(
            "DELETE FROM document_pages WHERE document_id = ? AND page_no = ?",
            (document_id, doc["page_count"]),
        )
        conn.execute(
            "UPDATE documents SET page_count = ? WHERE id = ?",
            (doc["page_count"] - 1, document_id),
        )
        return _document_dict(conn, document_id)


def finalize_document(db_path: str, document_id: int) -> dict[str, Any]:
    """把草稿提交为打印作业；重复提交返回同一作业。"""
    with db.transaction(db_path) as conn:
        doc = _get_document(conn, document_id)
        existing = conn.execute(
            "SELECT * FROM jobs WHERE document_id = ?", (document_id,)
        ).fetchone()
        if existing is not None:
            return _serialize_job(conn, existing)
        if doc["status"] != "draft":
            raise ServiceError("文档已经入队")
        if doc["page_count"] < 1:
            raise ServiceError("至少录入一页才能入队", status=400, code="empty_document")
        page_count = doc["page_count"]
        total_sheets = (page_count + 1) // 2
        ts = now_iso()
        conn.execute(
            "UPDATE documents SET status = 'ready', finalized_at = ? WHERE id = ?",
            (ts, document_id),
        )
        cur = conn.execute(
            """
            INSERT INTO jobs(document_id, status, page_count, total_sheets,
                             next_sheet, created_at)
            VALUES (?, 'queued', ?, ?, 1, ?)
            """,
            (document_id, page_count, total_sheets, ts),
        )
        job = _get_job(conn, cur.lastrowid)
        return _serialize_job(conn, job)


# --------------------------------------------------------------------------
# 打印机操作
# --------------------------------------------------------------------------


def start_job(db_path: str, job_id: int, printer_id: str) -> dict[str, Any]:
    with db.transaction(db_path) as conn:
        job = _get_job(conn, job_id)
        _require_printer(conn, printer_id)
        if job["status"] != "queued":
            raise ServiceError(f"作业当前为“{STATUS_LABELS.get(job['status'], job['status'])}”，不能开始")
        if not _printer_is_free(conn, printer_id):
            raise ServiceError("该打印机正忙", code="printer_busy")
        ts = now_iso()
        try:
            cur = conn.execute(
                """
                INSERT INTO print_segments(job_id, printer_id, kind, start_sheet,
                                           status, created_at)
                VALUES (?, ?, 'initial', 1, 'active', ?)
                """,
                (job_id, printer_id, ts),
            )
        except sqlite3.IntegrityError:
            raise ServiceError("该打印机正忙", code="printer_busy")
        segment = _require(conn, "SELECT * FROM print_segments WHERE id = ?", (cur.lastrowid,))
        conn.execute(
            "UPDATE jobs SET status = 'printing', started_at = ? WHERE id = ?",
            (ts, job_id),
        )
        _new_attempt(conn, _get_job(conn, job_id), segment, 1)
        return _serialize_job(conn, _get_job(conn, job_id))


def print_side(
    db_path: str,
    job_id: int,
    side: str,
    expected_sheet: Optional[int] = None,
) -> dict[str, Any]:
    """逐面打印当前纸张的正面或背面。"""
    if side not in ("front", "back"):
        raise ServiceError("side 必须是 front 或 back", status=400, code="invalid_side")
    with db.transaction(db_path) as conn:
        job = _get_job(conn, job_id)
        if job["status"] != "printing":
            raise ServiceError("作业不在打印中")
        segment = _active_segment(conn, job_id)
        if segment is None:
            raise ServiceError("找不到进行中的打印段")
        attempt = _in_progress_attempt(conn, job_id)
        if attempt is None:
            raise ServiceError("当前没有待打印的纸张")
        if expected_sheet is not None and expected_sheet != attempt["sheet_no"]:
            raise ServiceError(
                f"页序已变化，当前应打印第 {attempt['sheet_no']} 张",
                code="sheet_mismatch",
            )

        ts = now_iso()
        if side == "front":
            if attempt["front_printed"]:
                raise ServiceError("当前纸张正面已经打印")
            conn.execute(
                "UPDATE sheet_attempts SET front_printed = 1, front_printed_at = ? WHERE id = ?",
                (ts, attempt["id"]),
            )
        else:
            if not attempt["required_back"]:
                raise ServiceError("当前纸张背面留白，无需打印")
            if not attempt["front_printed"]:
                raise ServiceError("必须先打印正面")
            if attempt["back_printed"]:
                raise ServiceError("当前纸张背面已经打印")
            conn.execute(
                "UPDATE sheet_attempts SET back_printed = 1, back_printed_at = ? WHERE id = ?",
                (ts, attempt["id"]),
            )

        refreshed = _require(
            conn, "SELECT * FROM sheet_attempts WHERE id = ?", (attempt["id"],)
        )
        complete = bool(refreshed["front_printed"]) and (
            not refreshed["required_back"] or bool(refreshed["back_printed"])
        )
        if complete:
            # 两面都已出纸（或奇数页末张背面留白），这张纸才算完整出纸。
            conn.execute(
                "UPDATE sheet_attempts SET status = 'complete', ended_at = ? WHERE id = ?",
                (ts, attempt["id"]),
            )
            _advance_after_sheet(conn, job_id)
        return _serialize_job(conn, _get_job(conn, job_id))


def report_jam(db_path: str, job_id: int) -> dict[str, Any]:
    """报告卡纸：当前半张作废，原机进入待停稳状态，不派续单。"""
    with db.transaction(db_path) as conn:
        job = _get_job(conn, job_id)
        if job["status"] == "jammed":
            return _serialize_job(conn, job)
        if job["status"] != "printing":
            raise ServiceError("作业不在打印中，无法报告卡纸")
        segment = _active_segment(conn, job_id)
        if segment is None:
            raise ServiceError("找不到进行中的打印段")
        attempt = _in_progress_attempt(conn, job_id)
        ts = now_iso()
        if attempt is not None:
            conn.execute(
                """
                UPDATE sheet_attempts
                   SET status = 'scrapped', scrap_reason = '卡纸，半张作废', ended_at = ?
                 WHERE id = ?
                """,
                (ts, attempt["id"]),
            )
        conn.execute(
            "UPDATE print_segments SET status = 'jammed', ended_at = ?, note = ? WHERE id = ?",
            ("卡纸，等待原机停稳", ts, segment["id"]),
        )
        conn.execute("UPDATE jobs SET status = 'jammed' WHERE id = ?", (job_id,))
        return _serialize_job(conn, _get_job(conn, job_id))


def confirm_stopped(db_path: str, job_id: int) -> dict[str, Any]:
    """原机确认停稳；此前完整出纸的纸张保持有效。"""
    with db.transaction(db_path) as conn:
        job = _get_job(conn, job_id)
        if job["status"] == "stopped":
            return _serialize_job(conn, job)
        if job["status"] != "jammed":
            raise ServiceError("当前没有等待确认停稳的卡纸")
        segment = conn.execute(
            "SELECT * FROM print_segments WHERE job_id = ? AND status = 'jammed' "
            "ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if segment is None:
            raise ServiceError("找不到待停稳的打印段")
        ts = now_iso()
        # 只改打印段状态；sheet_attempts 里已完整出纸的记录一律保留。
        conn.execute(
            "UPDATE print_segments SET status = 'stopped', ended_at = ?, note = ? WHERE id = ?",
            ("已确认停稳，待换机续印", ts, segment["id"]),
        )
        conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = ?", (job_id,))
        return _serialize_job(conn, _get_job(conn, job_id))


def switch_job(
    db_path: str, job_id: int, printer_id: Optional[str] = None
) -> dict[str, Any]:
    """换机派续单。同一停稳段最多一份续单，重复调用返回同一份。"""
    with db.transaction(db_path) as conn:
        job = _get_job(conn, job_id)
        if job["status"] == "completed":
            raise ServiceError("作业已经完成")
        latest = _current_segment(conn, job_id)
        if latest is None:
            raise ServiceError("作业尚未开始打印")

        if latest["status"] == "jammed":
            raise ServiceError("原机尚未确认停稳，不能派续单", code="not_stopped")
        if latest["status"] == "active" and latest["kind"] == "initial":
            raise ServiceError("当前作业无需换机")
        if latest["status"] == "active":
            # 续单已经在打印，重复换机请求直接返回，不再派第二份。
            return _serialize_job(conn, job)
        if latest["status"] == "stopped" and job["status"] != "stopped":
            raise ServiceError("作业状态与打印段不一致")

        source = latest
        existing = conn.execute(
            "SELECT * FROM print_segments WHERE continued_from = ?", (source["id"],)
        ).fetchone()
        if existing is not None:
            return _serialize_job(conn, job)

        other = "P2" if source["printer_id"] == "P1" else "P1"
        target_id = printer_id or other
        _require_printer(conn, target_id)
        if target_id == source["printer_id"]:
            raise ServiceError("换机必须选择另一台打印机", status=400, code="same_printer")
        if not _printer_is_free(conn, target_id):
            raise ServiceError("备机正忙，无法接收续单", code="printer_busy")

        next_sheet = job["next_sheet"]
        if next_sheet > job["total_sheets"]:
            raise ServiceError("没有待续印的纸张")

        ts = now_iso()
        try:
            cur = conn.execute(
                """
                INSERT INTO print_segments(job_id, printer_id, kind, start_sheet,
                                           status, continued_from, created_at)
                VALUES (?, ?, 'continuation', ?, 'active', ?, ?)
                """,
                (job_id, target_id, next_sheet, source["id"], ts),
            )
        except sqlite3.IntegrityError:
            # 并发重复请求：已由另一个请求派单，取回现有记录。
            existing = conn.execute(
                "SELECT * FROM print_segments WHERE continued_from = ?", (source["id"],)
            ).fetchone()
            if existing is not None:
                return _serialize_job(conn, _get_job(conn, job_id))
            raise ServiceError("备机正忙，无法接收续单", code="printer_busy")

        segment = _require(conn, "SELECT * FROM print_segments WHERE id = ?", (cur.lastrowid,))
        conn.execute("UPDATE jobs SET status = 'printing' WHERE id = ?", (job_id,))
        # 半张已作废，续单从首张未完整出纸处重新打印两面。
        _new_attempt(conn, _get_job(conn, job_id), segment, next_sheet)
        return _serialize_job(conn, _get_job(conn, job_id))


# --------------------------------------------------------------------------
# 查询与序列化
# --------------------------------------------------------------------------


def _document_dict(conn: sqlite3.Connection, document_id: int) -> dict[str, Any]:
    doc = _get_document(conn, document_id)
    pages = [
        dict(p)
        for p in conn.execute(
            "SELECT page_no, content, added_at FROM document_pages "
            "WHERE document_id = ? ORDER BY page_no",
            (document_id,),
        )
    ]
    job = conn.execute("SELECT id FROM jobs WHERE document_id = ?", (document_id,)).fetchone()
    out = _row(doc)
    out["pages"] = pages
    out["job_id"] = job["id"] if job else None
    return out


def _serialize_job(conn: sqlite3.Connection, job: sqlite3.Row) -> dict[str, Any]:
    doc = conn.execute(
        "SELECT title FROM documents WHERE id = ?", (job["document_id"],)
    ).fetchone()
    out = _row(job)
    out["title"] = doc["title"] if doc else ""
    out["printed_sheets"] = conn.execute(
        "SELECT COUNT(*) FROM sheet_attempts WHERE job_id = ? AND status = 'complete'",
        (job["id"],),
    ).fetchone()[0]
    out["current_sheet"] = job["next_sheet"] if job["next_sheet"] <= job["total_sheets"] else None
    out["segments"] = [
        _segment_dict(conn, s)
        for s in conn.execute(
            "SELECT * FROM print_segments WHERE job_id = ? ORDER BY id", (job["id"],)
        )
    ]
    out["records"] = [
        _attempt_dict(conn, a)
        for a in conn.execute(
            "SELECT * FROM sheet_attempts WHERE job_id = ? ORDER BY id DESC", (job["id"],)
        )
    ]
    # 装订核对视图：只取完整出纸的纸张，按张号排序，给出正/背面页码。
    out["collation"] = [
        _attempt_dict(conn, a)
        for a in conn.execute(
            "SELECT * FROM sheet_attempts WHERE job_id = ? AND status = 'complete' "
            "ORDER BY sheet_no",
            (job["id"],),
        )
    ]
    return out


def _segment_dict(conn: sqlite3.Connection, seg: sqlite3.Row) -> dict[str, Any]:
    printer = conn.execute("SELECT name FROM printers WHERE id = ?", (seg["printer_id"],)).fetchone()
    out = _row(seg)
    out["printer_name"] = printer["name"] if printer else seg["printer_id"]
    out["label"] = STATUS_LABELS.get(seg["kind"], seg["kind"])
    out["status_label"] = STATUS_LABELS.get(seg["status"], seg["status"])
    return out


def _attempt_dict(conn: sqlite3.Connection, attempt: sqlite3.Row) -> dict[str, Any]:
    printer = conn.execute(
        "SELECT name FROM printers WHERE id = ?", (attempt["printer_id"],)
    ).fetchone()
    out = _row(attempt)
    out["printer_name"] = printer["name"] if printer else attempt["printer_id"]
    out["label"] = STATUS_LABELS.get(attempt["status"], attempt["status"])
    out["required_back"] = bool(attempt["required_back"])
    out["front_printed"] = bool(attempt["front_printed"])
    out["back_printed"] = bool(attempt["back_printed"])
    return out


def _printer_dict(conn: sqlite3.Connection, printer: sqlite3.Row) -> dict[str, Any]:
    seg = conn.execute(
        "SELECT * FROM print_segments WHERE printer_id = ? "
        "AND status IN ('active', 'jammed') ORDER BY id DESC LIMIT 1",
        (printer["id"],),
    ).fetchone()
    out = _row(printer)
    out["job_id"] = None
    out["job_title"] = None
    out["segment_id"] = None
    out["segment_status"] = None
    if seg is None:
        out["state"] = "idle"
    else:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (seg["job_id"],)).fetchone()
        doc = conn.execute(
            "SELECT title FROM documents WHERE id = ?", (job["document_id"],)
        ).fetchone()
        out["state"] = "printing" if seg["status"] == "active" else "jammed"
        out["job_id"] = seg["job_id"]
        out["job_title"] = doc["title"] if doc else ""
        out["segment_id"] = seg["id"]
        out["segment_status"] = seg["status"]
        out["next_sheet"] = job["next_sheet"]
        out["total_sheets"] = job["total_sheets"]
        attempt = _in_progress_attempt(conn, seg["job_id"])
        if attempt is not None and attempt["segment_id"] == seg["id"]:
            out["attempt"] = _attempt_dict(conn, attempt)
        else:
            out["attempt"] = None
    return out


def get_state(db_path: str) -> dict[str, Any]:
    with db.reading(db_path) as conn:
        documents = [
            _document_dict(conn, d["id"])
            for d in conn.execute("SELECT id FROM documents ORDER BY id DESC")
        ]
        jobs = [
            _serialize_job(conn, j)
            for j in conn.execute("SELECT * FROM jobs ORDER BY id DESC")
        ]
        printers = [
            _printer_dict(conn, p)
            for p in conn.execute("SELECT * FROM printers ORDER BY position")
        ]
        attempts = [
            _attempt_dict(conn, a)
            for a in conn.execute(
                "SELECT * FROM sheet_attempts ORDER BY id DESC LIMIT 500"
            )
        ]
        return {
            "documents": documents,
            "jobs": jobs,
            "printers": printers,
            "records": attempts,
            "server_time": now_iso(),
        }


def get_job(db_path: str, job_id: int) -> dict[str, Any]:
    with db.reading(db_path) as conn:
        return _serialize_job(conn, _get_job(conn, job_id))


def get_document(db_path: str, document_id: int) -> dict[str, Any]:
    with db.reading(db_path) as conn:
        return _document_dict(conn, document_id)
